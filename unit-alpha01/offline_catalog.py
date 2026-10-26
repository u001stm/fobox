"""Persistent shoot previews/state, verified source relinking, and card arrival events.

SQLite remains authoritative. JSON and JPEGs live on the computer, not on the SD.
No originals are moved, deleted, or replaced. A cached preview never qualifies as RAW.
"""
import base64
import hashlib
import io
import json
import os
import threading
import time
import uuid
from pathlib import Path


def fingerprint(path):
    p=Path(path);st=p.stat();h=hashlib.sha256(str(st.st_size).encode())
    with p.open('rb') as f:
        h.update(f.read(65536));f.seek(max(0,st.st_size-65536));h.update(f.read(65536))
    return h.hexdigest()


class OfflineCatalog:
    TABLES=('sessions','files','students','selections','selection_events','workflow_events',
            'ai_suggestions','anchor_overrides','marks','dividers','reason_ov','flag_ok',
            'renames','series_cat','approvals','faces','unit_projects','scene_boundaries',
            'lab_projects','lab_assets','lab_sources','lab_renders','lab_jobs')

    def __init__(self, engine):
        self.e=engine;self.root=engine.db_path.parent/'projects';self.root.mkdir(parents=True,exist_ok=True)
        self.stop=threading.Event();self.lock=threading.RLock();self.dirty=set();self.events=[]
        self.seen={};self.warming=set();self.statuses={};self.last_snapshot={};self._cache={};self.started=False
        self._records={};self._roots={};self.workers=[];self.monitor=None
        with self.e.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS offline_files(session_id TEXT,path TEXT,relative_path TEXT,signature TEXT,preview_name TEXT,preview_size INTEGER DEFAULT 0,PRIMARY KEY(session_id,path))')
            c.execute('CREATE TABLE IF NOT EXISTS offline_sources(session_id TEXT PRIMARY KEY,original_root TEXT,current_root TEXT)')
            if 'provider_signature' not in {r[1] for r in c.execute('PRAGMA table_info(offline_files)')}:
                c.execute('ALTER TABLE offline_files ADD COLUMN provider_signature TEXT')

    def project_dir(self,sid):
        if not sid or not all(x in 'abcdef0123456789' for x in sid):raise ValueError('Неверный ID съёмки')
        return self.root/sid

    def record(self,sid,path):
        if sid not in self._records:
            with self.lock,self.e.conn() as c:
                self._records[sid]={r['path']:dict(r) for r in c.execute('SELECT * FROM offline_files WHERE session_id=?',(sid,))}
        return self._records[sid].get(path)

    def roots(self,sid):
        if sid in self._roots:return self._roots[sid]
        with self.e.conn() as c:
            r=c.execute('SELECT original_root,current_root FROM offline_sources WHERE session_id=?',(sid,)).fetchone()
            ss=c.execute('SELECT source FROM sessions WHERE id=?',(sid,)).fetchone()
        old=(r['original_root'] if r else None) or (ss['source'] if ss else None)
        value=Path(old) if old else None,Path(r['current_root']) if r else Path(old) if old else None
        # Do not cache a missing session while import is still creating it.
        if old:self._roots[sid]=value
        return value

    def resolve(self,sid,path):
        old,current=self.roots(sid)
        if not old or not current:return str(path)
        try:return str(current/Path(path).relative_to(old))
        except ValueError:return str(path)

    def available(self,sid,path,fresh=False):
        actual=self.resolve(sid,path)
        r=self.record(sid,path)
        try:
            st=os.stat(actual);key=(sid,path,actual,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
            hit=self._cache.get(key)
            if not fresh and hit and time.monotonic()-hit[1]<2:return hit[0]
            if r:
                ok=fingerprint(actual)==r['signature']
            else:
                with self.e.conn() as c:f=c.execute('SELECT bytes,capture_at,preview FROM files WHERE session_id=? AND path=?',(sid,path)).fetchone()
                ok=bool(f and self.e._src_ok(actual,f['bytes'],f['capture_at'],f['preview']))
            self._cache[key]=(ok,time.monotonic())
            if len(self._cache)>6000:self._cache.clear()
            return ok
        except OSError:return False

    def preview(self,sid,path,size):
        r=self.record(sid,path)
        cached=self.project_dir(sid)/'previews'/r['preview_name'] if r and r['preview_name'] else None
        provider_ok=True
        if r and r.get('provider_signature'):
            with self.e.conn() as c:provider=c.execute('SELECT preview FROM files WHERE session_id=? AND path=?',(sid,path)).fetchone()
            try:provider_ok=bool(provider and fingerprint(self.resolve(sid,provider['preview']))==r['provider_signature'])
            except OSError:provider_ok=False
        if cached and cached.is_file() and (r['preview_size']>=size or not provider_ok or not self.available(sid,path)):
            data=cached.read_bytes()
            if size>=r['preview_size']:return data
            try:
                from PIL import Image
                im=Image.open(io.BytesIO(data));im.thumbnail((size,size));b=io.BytesIO();im.save(b,'JPEG',quality=78);return b.getvalue()
            except Exception:return data
        # Never read another card into a project's persistent cache.
        if r and not self.available(sid,path):return None
        if not r:
            old,current=self.roots(sid)
            if not current or not current.is_dir():return None
            try:
                identity=self.e.session_id(current)
                with self.e.conn() as c:
                    ss=c.execute('SELECT company FROM sessions WHERE id=?',(sid,)).fetchone()
                    managed=c.execute('SELECT bytes FROM lab_assets WHERE session_id=? AND path=?',(sid,path)).fetchone() if ss and ss['company']=='unitlab' else None
                if ss and ss['company']=='unitlab':
                    # UUID sessions use an explicitly verified managed-asset registry, not a card-path identity.
                    if not managed or Path(self.resolve(sid,path)).stat().st_size!=managed['bytes']:return None
                else:
                    expected=hashlib.sha1(('unit|'+identity).encode()).hexdigest()[:12] if ss and ss['company']=='unit' else identity
                    if sid!=expected:return None
            except OSError:return None
        try:
            before_signature=fingerprint(self.resolve(sid,path))
        except OSError:return None
        data=self.e._live_preview_bytes(sid,path,size)
        if data:
            try:self.store(sid,path,data,size,expected_signature=before_signature)
            except OSError:pass # a full disk must not erase existing state
        return data

    def store(self,sid,path,data,size,expected_signature=None):
        with self.lock:
            return self._store(sid,path,data,size,expected_signature)

    def _store(self,sid,path,data,size,expected_signature=None):
        actual=Path(self.resolve(sid,path));signature=fingerprint(actual)
        if expected_signature and signature!=expected_signature:return
        old,current=self.roots(sid)
        try:rel=str(Path(path).relative_to(old))
        except (ValueError,TypeError):return
        prior=self.record(sid,path)
        if prior and signature!=prior['signature']:return
        if prior and prior['preview_size']>=size and prior['preview_name'] and (self.project_dir(sid)/'previews'/prior['preview_name']).exists():return
        folder=self.project_dir(sid)/'previews';folder.mkdir(parents=True,exist_ok=True)
        name=hashlib.sha256(str(path).encode()).hexdigest()+'.jpg'
        tmp=folder/(name+'.'+uuid.uuid4().hex+'.tmp');tmp.write_bytes(data);os.replace(tmp,folder/name)
        provider_signature=None
        with self.e.conn() as c:provider=c.execute('SELECT preview FROM files WHERE session_id=? AND path=?',(sid,path)).fetchone()
        if provider and provider['preview'] and not provider['preview'].startswith('embedded:'):
            try:provider_signature=fingerprint(self.resolve(sid,provider['preview']))
            except OSError:pass
        with self.e.conn() as c:
            c.execute('INSERT OR REPLACE INTO offline_files VALUES(?,?,?,?,?,?,?)',(sid,path,rel,signature,name,size,provider_signature))
        self._records.setdefault(sid,{})[path]={'session_id':sid,'path':path,'relative_path':rel,'signature':signature,
                                               'preview_name':name,'preview_size':size,'provider_signature':provider_signature}
        self.dirty.add(sid)

    def revision(self,sid,path,preview,orient):
        r=self.record(sid,path)
        if r and r['preview_name']:return 'offline-'+r['signature'][:16]+'-'+str(r['preview_size'])
        return self.e.preview_revision(sid,self.resolve(sid,path),preview,orient)

    def snapshot(self,sid):
        """Consistent transaction; no global settings, credentials, or profiles in JSON."""
        out={'schema':'lumio.project.v1','session_id':sid,'saved_at':time.strftime('%Y-%m-%dT%H:%M:%S'),'tables':{}}
        with self.e.conn() as c:
            c.execute('BEGIN')
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in self.TABLES:
                if table not in tables:continue
                column='id' if table=='sessions' else 'session_id'
                rows=[]
                for r in c.execute(f'SELECT * FROM {table} WHERE {column}=?',(sid,)):
                    rows.append({k:({'encoding':'base64','value':base64.b64encode(v).decode()} if isinstance(v,bytes) else v) for k,v in dict(r).items()})
                out['tables'][table]=rows
            if not out['tables'].get('sessions'):return
            out['sources']=[dict(r) for r in c.execute('SELECT * FROM offline_sources WHERE session_id=?',(sid,))]
            out['assets']=[dict(r) for r in c.execute('SELECT * FROM offline_files WHERE session_id=?',(sid,))]
        folder=self.project_dir(sid);folder.mkdir(parents=True,exist_ok=True)
        with self.lock:
            tmp=folder/'project.json.tmp'
            with tmp.open('w',encoding='utf-8') as f:
                json.dump(out,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
            tmp.replace(folder/'project.json')
        self.last_snapshot[sid]=time.time()
        return str(folder/'project.json')

    def remember_source(self,sid,root):
        with self.e.conn() as c:
            original=c.execute('SELECT source FROM sessions WHERE id=?',(sid,)).fetchone()
            original_root=original['source'] if original and original['source'] else str(root)
            c.execute('INSERT INTO offline_sources VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET current_root=excluded.current_root',
                      (sid,original_root,str(root)))
        self._roots.pop(sid,None)

    def warm(self,sid):
        with self.lock:
            if sid in self.warming:return
            self.warming.add(sid)
        def run():
            done=0;missing=[]
            try:
                with self.e.conn() as c:rows=[dict(r) for r in c.execute('SELECT path FROM files WHERE session_id=? ORDER BY path',(sid,))]
                self.statuses[sid]={'stage':'Сохраняю превью на компьютер','total':len(rows),'done':0,'finished':False}
                for r in rows:
                    if self.stop.is_set():break
                    data=self.preview(sid,r['path'],1280)
                    if data:done+=1
                    else:missing.append(Path(r['path']).name)
                    self.statuses[sid].update(done=done)
                self.snapshot(sid)
                self.statuses[sid].update(finished=True,missing=missing,stage='Превью сохранены' if not missing and done==len(rows) else 'Не все превью доступны')
            except Exception as error:self.statuses[sid]={'finished':True,'done':done,'error':str(error)}
            finally:
                with self.lock:self.warming.discard(sid)
        worker=threading.Thread(target=run,daemon=True);self.workers.append(worker);worker.start()

    def verify_root(self,sid,root):
        root=Path(root).expanduser().resolve()
        with self.e.conn() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM offline_files WHERE session_id=? ORDER BY relative_path',(sid,))]
        if not rows:return False
        # Verify every registered original before accepting relocation. Additions are allowed.
        try:return all(fingerprint(root/r['relative_path'])==r['signature'] for r in rows)
        except OSError:return False

    def relink(self,sid,root):
        root=Path(root).expanduser().resolve()
        if not self.verify_root(sid,root):raise ValueError('Это другая или неполная съёмка. Файлы не совпали с сохранёнными отпечатками.')
        self.remember_source(sid,root);self._cache.clear();self.dirty.add(sid);self.warm(sid)
        return {'ok':True,'session':sid,'source':str(root)}

    def state(self,sid):
        old,current=self.roots(sid)
        with self.e.conn() as c:
            total=c.execute('SELECT COUNT(*) FROM files WHERE session_id=?',(sid,)).fetchone()[0]
            records=[dict(r) for r in c.execute('SELECT * FROM offline_files WHERE session_id=?',(sid,))]
        ready=sum(bool(r['preview_name']) and (self.project_dir(sid)/'previews'/r['preview_name']).is_file() for r in records)
        # The current root must match the recorded shoot, not merely exist at D:\DCIM.
        sample=records[::max(1,len(records)//6)][:6]
        try:online=bool(sample and current and all(fingerprint(current/r['relative_path'])==r['signature'] for r in sample))
        except OSError:online=False
        return {'session':sid,'online':online,'cached':ready,'total':total,'ready':ready==total and total>0,
                'job':self.statuses.get(sid),'snapshot':str(self.project_dir(sid)/'project.json'),
                'source':str(current) if current else None}

    def passport(self,sid):
        old,current=self.roots(sid)
        if not current or not self.verify_root(sid,current):raise ValueError('Подключите карту именно этой съёмки')
        with self.e.conn() as c:ss=c.execute('SELECT title FROM sessions WHERE id=?',(sid,)).fetchone()
        target=current/'.LUMIO.json'
        if target.exists():
            prior=json.loads(target.read_text('utf-8'))
            if prior.get('session_id')!=sid:raise ValueError('На карте уже есть паспорт другой съёмки; он сохранён без изменений')
        data={'schema':'lumio.card.v1','session_id':sid,'title':ss['title'],'note':'Project and previews are on the computer; this file is only an identity hint.'}
        tmp=target.with_suffix('.json.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8');tmp.replace(target)
        return {'path':str(target)}

    def candidates(self):
        with self.e.conn() as c:roots={r[0] for r in c.execute('SELECT source FROM sessions WHERE source IS NOT NULL')}
        if os.name=='nt':
            import ctypes
            mask=ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                drive=chr(65+i)+':\\'
                if mask&(1<<i) and ctypes.windll.kernel32.GetDriveTypeW(drive)==2:
                    dcim=Path(drive)/'DCIM'
                    if dcim.is_dir():
                        roots.update(str(p) for p in dcim.iterdir() if p.is_dir())
        return [Path(p) for p in roots if Path(p).is_dir()]

    def scan(self):
        """Only small anchor fingerprints are polled. Reconnection is fully verified separately."""
        with self.e.conn() as c:
            sessions=[dict(r) for r in c.execute('SELECT id,source,company,title,meta FROM sessions')]
            records=[dict(r) for r in c.execute('SELECT * FROM offline_files ORDER BY session_id,relative_path')]
        anchors={}
        for r in records:
            if len(anchors.setdefault(r['session_id'],[]))<6:anchors[r['session_id']].append(r)
        active={}
        for root in self.candidates():
            key=str(root);matches=[];signatures=[]
            for ss in sessions:
                sid=ss['id'];sample=anchors.get(sid)
                if sample:
                    try:
                        signs=[fingerprint(root/r['relative_path']) for r in sample]
                        if signs==[r['signature'] for r in sample]:matches.append(ss);signatures=signs
                    except OSError:pass
                elif str(root)==ss['source']:
                    try:
                        digest=self.e.session_id(root)
                        expected=hashlib.sha1(('unit|'+digest).encode()).hexdigest()[:12] if ss['company']=='unit' else digest
                        if sid==expected:
                            self.remember_source(sid,root);self.warm(sid);matches.append(ss);signatures=[digest]
                    except OSError:pass
            # A different card at the same path must be a different insertion event.
            try:
                images=[p for p in sorted(root.iterdir()) if p.is_file() and p.suffix.lower() in ('.arw','.cr2','.cr3','.nef','.raf','.dng','.rw2','.orf','.jpg','.jpeg')]
                probes=[images[i] for i in sorted({0,len(images)//2,len(images)-1})] if images else []
                stamp='|'.join(fingerprint(p) for p in probes)+'|'+str(len(images))+'|'+','.join(sorted(s['id'] for s in matches))
            except OSError:continue
            active[key]=stamp
            if self.seen.get(key)!=stamp:
                for ss in matches:
                    # Full validation is needed for changed drive letters. Original aliases stay stable.
                    old,current=self.roots(ss['id'])
                    if current!=root and anchors.get(ss['id']):
                        try:self.relink(ss['id'],root)
                        except ValueError:continue
                    else:self.warm(ss['id'])
                self.events.append({'id':max(int(time.time()*1000),(self.events[-1]['id']+1) if self.events else 0),'source':key,'sessions':[{'id':s['id'],'title':s['title'],'company':s['company']} for s in matches],
                                    'kind':'known' if matches else 'new'})
                self.events=self.events[-20:]
        self.seen=active

    def start(self):
        if self.started:return
        self.started=True
        with self.e.conn() as c:self.dirty.update(r[0] for r in c.execute('SELECT id FROM sessions'))
        def run():
            while not self.stop.is_set():
                try:
                    self.scan()
                    for sid in list(self.dirty):
                        if time.time()-self.last_snapshot.get(sid,0)>=3:
                            self.dirty.discard(sid);self.snapshot(sid)
                except Exception as error:self.last_error=str(error)
                self.stop.wait(3)
        self.monitor=threading.Thread(target=run,daemon=True);self.monitor.start()

    def close(self):
        self.stop.set()
        if self.monitor:self.monitor.join(timeout=4)
        for worker in self.workers:
            if worker is not threading.current_thread():worker.join(timeout=4)

    def api_state(self,sid=None):
        return {'project':self.state(sid) if sid else None,'events':self.events[-10:],
                'error':getattr(self,'last_error',None),
                'ui_active':time.time()-getattr(self,'ui_seen',0)<9,
                'autostart':(self.e.db_path.parent/'card-watch.enabled').exists()}
