"""FOBOX 3.0 Preview. Local review of school RAW; no cloud services."""
import argparse,csv,json,re,shutil,threading,webbrowser,mimetypes
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs

RAW={'.arw','.cr2','.cr3','.nef','.nrw','.raf','.rw2','.orf','.dng','.pef','.3fr','.srw','.iiq','.rwl','.erf','.k25','.kdc','.mos','.mrw','.x3f'}
IMG={'.jpg','.jpeg','.png','.webp'}
CATS=['01_ДОКУМЕНТЫ','02_ЗДАНИЕ','03_ОБЩАЯ','04_РЕПОРТАЖ','05_УЧЕНИКИ','06_УЧИТЕЛЯ','07_ФОН']
def key(stem):
    m=re.search(r'(\d+)$',stem)
    return str(int(m[1])) if m else None
def safe(s):
    s=re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',str(s)).strip(' .')
    if not s:raise ValueError('Empty name')
    return s
def csvrows(p,cols):
    if not Path(p).exists():return []
    with open(p,encoding='utf-8-sig',newline='') as f:
        r=csv.DictReader(f)
        if not set(cols)<=set(r.fieldnames or []):raise ValueError('CSV columns: '+','.join(cols))
        return list(r)
class Project:
    def __init__(self,a):
        self.a=a;self.src=Path(a.source).resolve();self.dest=Path(a.output).resolve()
        if not self.src.is_dir():raise ValueError('Source folder not found')
        if self.dest==self.src or self.src in self.dest.parents:raise ValueError('Output inside source')
        self.raw={};self.bykey={};self.jpg={};self.conflicts=[]
        for p in self.src.rglob('*'):
            if not p.is_file():continue
            if p.suffix.lower() in RAW:
                k=str(p.resolve());self.raw[k]=p
                if key(p.stem):self.bykey.setdefault(key(p.stem),[]).append(p)
            elif p.suffix.lower() in IMG:self.jpg.setdefault(p.stem.lower(),[]).append(p)
        self.students=csvrows(a.students,['Name','V','P1','P2','D1','D2'])
        self.teachers=csvrows(a.teachers,['Name','Role','Start','End'])
        self.sections=[];self.frames={}
        for i,row in enumerate(self.students):
            name=safe(' '.join(row['Name'].split()[:2]))
            for suffix,cols,kind in [('p',['V','P1','P2'],'portrait'),('f',['D1','D2'],'friends')]:
                anchors=[self.resolve(row[c]) for c in cols]
                near=self.nearby(anchors)
                self.section(f's{i}{suffix}',name+(' — Портреты / виньетка' if suffix=='p' else ' — Друзья'),kind,anchors,near)
        for i,row in enumerate(self.teachers):
            first=self.resolve(row['Start']);last=self.resolve(row['End'])
            series=[]
            if first and last:
                lo,hi=sorted([int(key(first.stem)),int(key(last.stem))])
                series=[p for p in self.raw.values() if key(p.stem) and lo<=int(key(p.stem))<=hi]
            self.section(f't{i}',safe(row['Name'])+' — '+safe(row['Role']),'teacher',[],series,5)
        self.statepath=Path(a.state).resolve()
        self.state={'selected':{},'approved':{},'background':''}
        if self.statepath.exists():
            saved=json.loads(self.statepath.read_text(encoding='utf-8'))
            for k in self.state:self.state[k]=saved.get(k,self.state[k])
        for s in self.sections:
            self.state['selected'].setdefault(s['id'],[f['id'] for f in self.frames[s['id']] if f['mandatory']])
        self.save()
    def resolve(self,val):
        val=str(val or '').strip()
        if not val:return None
        k=key(Path(val).stem)
        matches=self.bykey.get(k,[]) if k else []
        if len(matches)==1:return matches[0]
        exact=[p for p in matches if p.name.lower()==val.lower() or p.stem.lower()==Path(val).stem.lower()]
        if len(exact)==1:return exact[0]
        self.conflicts.append('Missing/ambiguous RAW: '+val)
        return None
    def nearby(self,anchors):
        ns=[int(key(p.stem)) for p in anchors if p and key(p.stem)]
        if not ns:return []
        return [p for p in self.raw.values() if key(p.stem) and min(abs(int(key(p.stem))-n) for n in ns)<=self.a.radius]
    def section(self,sid,title,kind,anchors,near,limit=None):
        must={str(p.resolve()) for p in anchors if p}
        unique={str(p.resolve()):p for p in near+anchors if p}
        frames=[]
        for k,p in sorted(unique.items(),key=lambda x:x[1].name):
            j=self.jpg.get(p.stem.lower(),[])
            preview=next((x for x in j if x.parent==p.parent),j[0] if len(j)==1 else None)
            frames.append({'id':k,'name':p.name,'mandatory':k in must,'preview':str(preview) if preview else None})
        self.sections.append({'id':sid,'title':title,'kind':kind,'limit':limit,'count':len(frames)})
        self.frames[sid]=frames
    def save(self):
        self.statepath.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.statepath.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(self.statepath)
    def summary(self):
        return {'order':self.a.order,'school':self.a.school,'date':self.a.date,'class':self.a.class_name,
                'sections':self.sections,'frames':self.frames,'state':self.state,'conflicts':self.conflicts,'raw_count':len(self.raw)}
    def review(self,b):
        sid=b['section']
        if sid not in self.frames:raise ValueError('Unknown section')
        chosen=b['selected']
        allowed={f['id'] for f in self.frames[sid]}
        if not isinstance(chosen,list) or not set(chosen)<=allowed:raise ValueError('Invalid selected frames')
        s=next(x for x in self.sections if x['id']==sid)
        if s['limit'] and len(chosen)>s['limit']:raise ValueError('Maximum 5 teacher frames')
        if b.get('approved') and any(f['mandatory'] and f['id'] not in chosen for f in self.frames[sid]):
            raise ValueError('Mandatory frames must remain selected; correct worksheet if wrong')
        self.state['selected'][sid]=chosen;self.state['approved'][sid]=bool(b.get('approved'));self.save()
    def background(self,b):
        path=str(b.get('path','')).strip()
        if path and not Path(path).is_file():raise ValueError('Background file not found')
        self.state['background']=str(Path(path).resolve()) if path else '';self.save()
    def export(self):
        if self.conflicts:raise ValueError('Resolve missing or ambiguous worksheet frames first')
        if any(not self.state['approved'].get(s['id']) for s in self.sections):
            raise ValueError('Confirm every review section before export')
        root=self.dest/safe(self.a.order)/safe(self.a.school)/safe(self.a.date)/safe(self.a.class_name)
        if root.exists() and any(root.iterdir()):raise ValueError('Output order folder must be empty')
        ops=[];names=set()
        def add(p,relative):
            d=root/relative
            if str(d).casefold() in names:raise ValueError('Filename collision: '+str(d))
            names.add(str(d).casefold());ops.append((p,d))
        for i,row in enumerate(self.students):
            name=safe(' '.join(row['Name'].split()[:2]));required={}
            for col,prefix in [('V',name+'_'),('P1','ЛР_'+name+'_'),('P2','ЛС_'+name+'_'),('D1','Друг1_'),('D2','Друг2_')]:
                p=self.resolve(row[col])
                if p:required[str(p.resolve())]=prefix+p.name
            ids=set(self.state['selected'].get(f's{i}p',[])+self.state['selected'].get(f's{i}f',[]))
            for k in ids:
                p=self.raw[k];add(p,Path(CATS[4])/name/required.get(k,p.name))
        for i,row in enumerate(self.teachers):
            ids=self.state['selected'].get(f't{i}',[])
            if len(ids)>5:raise ValueError('Too many teacher frames')
            for k in ids:
                p=self.raw[k]
                filename=p.name if len(ids)>1 else safe(row['Name']+' '+row['Role'])+p.suffix
                add(p,Path(CATS[5])/filename)
        if self.state['background']:
            p=Path(self.state['background']);add(p,Path(CATS[6])/p.name)
        if not ops:raise ValueError('No files selected')
        for p,d in ops:d.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,d)
        for cat in CATS:(root/cat).mkdir(parents=True,exist_ok=True)
        return {'files':len(ops),'root':str(root)}
PAGE=r'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>FOBOX 3.0</title><style>
*{box-sizing:border-box}body{margin:0;background:#161616;color:#eee;font:14px system-ui}header{padding:18px 24px;border-bottom:1px solid #444}header b{font-size:23px}main{display:grid;grid-template-columns:260px 1fr;height:calc(100vh - 76px)}aside{overflow:auto;border-right:1px solid #444;padding:12px}aside button{display:block;width:100%;background:#252525;color:#eee;border:0;border-radius:5px;padding:12px;text-align:left;margin:4px 0;cursor:pointer}.active{background:#525252!important}.done:before{content:'✓ ';color:#8d9}section{display:flex;flex-direction:column;min-width:0}.head,.foot{padding:15px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #333}.foot{border-top:1px solid #333;border-bottom:0}h2{margin:0}.grid{padding:15px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;overflow:auto;align-content:start}.tile{cursor:pointer;background:#242424;border:2px solid #444;border-radius:6px;overflow:hidden;position:relative}.tile.on{border-color:#7dba96}.tile img,.placeholder{width:100%;height:170px;object-fit:contain;display:grid;place-items:center}.tile span{display:block;padding:7px;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tile.on:before{content:'✓';position:absolute;right:8px;top:8px;background:#297c53;border-radius:50%;padding:2px 6px}.tile.required:after{content:'★';position:absolute;left:8px;top:6px;color:#ffce6d}button.action{background:#eee;color:#111;border:0;border-radius:5px;padding:11px 15px;cursor:pointer}input{width:100%;padding:9px;background:#222;color:white;border:1px solid #555}@media(min-width:1600px){.grid{grid-template-columns:repeat(6,minmax(0,1fr))}}
</style><header><b>FOBOX 3.0</b> <span id="order"></span> <span id="msg" style="float:right">Локальная проверка</span></header><main><aside id="nav"></aside><section><div class="head"><h2 id="title"></h2><button class="action" onclick="next()">Следующий →</button></div><div class="grid" id="grid"></div><div class="foot"><b id="count"></b><div><button class="action" onclick="save(false)">Сохранить</button> <button class="action" onclick="save(true)">Подтвердить ✓</button></div></div></section></main><script>
let d,i=0,selected=new Set();const $=x=>document.getElementById(x);async function api(path,b){let r=await fetch(path,{method:b===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:b===undefined?undefined:JSON.stringify(b)}),j=await r.json();if(!r.ok)throw Error(j.error);return j}function message(x){$('msg').textContent=x}
function render(){if(!d.sections.length){$('title').textContent='Нет строк в ведомости';return}let s=d.sections[i];selected=new Set(d.state.selected[s.id]||[]);$('title').textContent=s.title;$('nav').replaceChildren();d.sections.forEach((v,n)=>{let b=document.createElement('button');b.textContent=v.title;b.className=(n===i?'active ':'')+(d.state.approved[v.id]?'done':'');b.onclick=()=>{i=n;render()};$('nav').append(b)});let inp=document.createElement('input');inp.placeholder='Полный путь к фону';inp.value=d.state.background||'';$('nav').append(inp);let bg=document.createElement('button');bg.textContent='Сохранить фон';bg.onclick=async()=>{try{await api('/background',{path:inp.value});message('Фон сохранён')}catch(e){message(e.message)}};$('nav').append(bg);let ex=document.createElement('button');ex.textContent='Собрать заказ';ex.onclick=async()=>{if(!confirm('Копировать подтверждённые RAW?'))return;try{let x=await api('/export',{});message('Скопировано '+x.files+' файлов')}catch(e){alert(e.message)}};$('nav').append(ex);if(d.conflicts.length){let w=document.createElement('p');w.style.color='#eaa';w.textContent=d.conflicts.join('; ');$('nav').append(w)}
$('grid').replaceChildren();d.frames[s.id].forEach(f=>{let t=document.createElement('div');t.className='tile'+(selected.has(f.id)?' on':'')+(f.mandatory?' required':'');if(f.preview){let im=document.createElement('img');im.src='/image?path='+encodeURIComponent(f.preview);im.loading='lazy';t.append(im)}else{let p=document.createElement('div');p.className='placeholder';p.textContent='Нет JPEG';t.append(p)}let cap=document.createElement('span');cap.textContent=f.name;t.append(cap);t.onclick=()=>{if(selected.has(f.id))selected.delete(f.id);else{if(s.limit&&selected.size>=s.limit){message('Максимум '+s.limit+' кадров');return}selected.add(f.id)}t.classList.toggle('on',selected.has(f.id));d.state.approved[s.id]=false;count()};$('grid').append(t)});count()}
function count(){$('count').textContent=selected.size+' / '+d.sections[i].count+' выбрано'}
async function save(approved){let s=d.sections[i];try{await api('/review',{section:s.id,selected:[...selected],approved});d.state.selected[s.id]=[...selected];d.state.approved[s.id]=approved;message(approved?'Подтверждено':'Сохранено');if(approved)next();else render()}catch(e){message(e.message)}}function next(){i=(i+1)%d.sections.length;render()}api('/state').then(x=>{d=x;$('order').textContent=[d.order,d.school,d.date,d.class].join(' / ');render()}).catch(e=>message(e.message))
</script></html>'''
class Handler(BaseHTTPRequestHandler):
    project=None
    def reply(self,status,obj):
        b=json.dumps(obj,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/':
            b=PAGE.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
        elif u.path=='/state':self.reply(200,self.project.summary())
        elif u.path=='/image':
            p=parse_qs(u.query).get('path',[''])[0]
            allowed={f['preview'] for fs in self.project.frames.values() for f in fs if f['preview']}
            if p not in allowed:self.reply(404,{'error':'Unknown preview'});return
            b=Path(p).read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(p)[0] or 'image/jpeg');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
        else:self.reply(404,{'error':'Not found'})
    def do_POST(self):
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n>1000000:raise ValueError('Too large')
            b=json.loads(self.rfile.read(n))
            if self.path=='/review':self.project.review(b);self.reply(200,{'ok':True})
            elif self.path=='/background':self.project.background(b);self.reply(200,{'ok':True})
            elif self.path=='/export':self.reply(200,self.project.export())
            else:self.reply(404,{'error':'Not found'})
        except (ValueError,KeyError,OSError) as e:self.reply(400,{'error':str(e)})
    def log_message(self,*a):pass
def main():
    p=argparse.ArgumentParser()
    for name in ['source','output','order','school','date','class-name']:p.add_argument('--'+name,required=True)
    p.add_argument('--students',default='students.csv');p.add_argument('--teachers',default='teachers.csv')
    p.add_argument('--state',default='fobox3_state.json');p.add_argument('--radius',type=int,default=12)
    p.add_argument('--port',type=int,default=8765)
    a=p.parse_args();pr=Project(a);Handler.project=pr
    server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler)
    url=f'http://127.0.0.1:{a.port}/';print('FOBOX local review:',url,'RAW:',len(pr.raw),flush=True)
    threading.Timer(1,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
if __name__=='__main__':main()
