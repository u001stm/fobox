"""UNIT alpha01. Independent workspace; original files and legacy selections are read-only."""
import hashlib
import io
import json
import shutil
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

CHANNELS = {'unit': 'UNIT · репортаж', 'model': 'Модельная съёмка', 'startup': 'Стартап Фото',
            'vypusk': 'Выпуск', 'crous': 'Crous', 'other': 'Другое'}
MEDIA = {'.mp4': 'video', '.mov': 'video', '.m4v': 'video', '.webm': 'video',
         '.wav': 'audio', '.mp3': 'audio', '.m4a': 'audio', '.ogg': 'audio', '.flac': 'audio'}


def cluster_faces(frames, threshold=.45, min_height=.07, min_score=.80):
    """All faces, not just portraits. No two faces in one image may share an identity.

    Centroid AND fixed exemplar must agree, preventing unbounded similarity chains.
    Single-frame identities remain visible, separately from repeated identities.
    """
    import numpy as np
    groups = []
    for path, faces in sorted(frames.items()):
        used = set()
        for f in sorted(faces, key=lambda x: -x.get('score', 0)):
            if f.get('score', 0) < min_score or f.get('h', 0) < min_height:
                continue
            e = np.asarray(f['emb'], dtype='float32')
            if e.shape != (512,) or not np.isfinite(e).all() or np.linalg.norm(e) < .01:
                continue
            e = e / np.linalg.norm(e)
            candidates = [(float(e @ g['center']), i) for i, g in enumerate(groups)
                          if i not in used and float(e @ g['first']) >= threshold]
            score, idx = max(candidates, default=(-1, -1))
            if score < threshold:
                idx = len(groups)
                groups.append({'first': e.copy(), 'center': e.copy(), 'sum': e.copy(),
                               'paths': [path], 'box': f.get('box'), 'iw': f.get('iw'), 'ih': f.get('ih')})
            else:
                g = groups[idx]
                g['sum'] += e
                g['center'] = g['sum'] / np.linalg.norm(g['sum'])
                g['paths'].append(path)
            used.add(idx)
    return [{'id': i + 1, 'path': g['paths'][0], 'paths': g['paths'], 'appearances': len(g['paths']),
             'box': g['box'], 'iw': g['iw'], 'ih': g['ih']} for i, g in enumerate(groups)]


class UnitWorkspace:
    def __init__(self, engine):
        self.e = engine
        self.cache = {}
        with self.e.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS unit_projects(session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL)')
            cfg = {'layout': 'unit', 'short': 'U', 'color': '#555555', 'note': 'UNIT α01 · репортаж и мультимедиа'}
            c.execute('INSERT OR IGNORE INTO companies(id,name,config,sort) VALUES(?,?,?,?)',
                      ('unit', 'UNIT α01', json.dumps(cfg, ensure_ascii=False), 10))

    def rows(self, sid):
        with self.e.conn() as c:
            ss = c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not ss or ss['company'] not in ('unit', 'unitlab'):
                raise ValueError('Это не проект UNIT')
            files = [dict(r) for r in c.execute('SELECT * FROM files WHERE session_id=? ORDER BY capture_at,name,path', (sid,))]
        return dict(ss), files

    def state(self, sid):
        session, _ = self.rows(sid)
        with self.e.conn() as c:
            r = c.execute('SELECT * FROM unit_projects WHERE session_id=?', (sid,)).fetchone()
        return (json.loads(r['data']), r['revision']) if r else ({'channel': 'unit', 'selected': [], 'items': [],
            'jpeg': {}, 'media': {}, 'threshold': .55 if session['company']=='unitlab' else .45, 'rejected': []}, 0)

    def save(self, sid, state, revision):
        session, files = self.rows(sid)
        if session['company'] == 'unitlab':
            state = {**state, 'channel': 'unit'}
        paths = {f['path'] for f in files}
        if state.get('channel') not in CHANNELS:
            raise ValueError('Неизвестный канал')
        selected = state.get('selected', [])
        if len(selected) != len(set(selected)) or not set(selected) <= paths:
            raise ValueError('Неверный список выбранных кадров')
        if not set(state.get('rejected', [])) <= paths:
            raise ValueError('Неверный список брака')
        threshold = float(state.get('threshold', .45))
        if not .4 <= threshold <= .85:
            raise ValueError('Строгость: 0.40–0.85')
        items = state.get('items', [])
        if len(items) > 10000 or len({x.get('id') for x in items}) != len(items):
            raise ValueError('Повторяющиеся элементы сборки')
        # File paths can only enter via registered catalog/media, never arbitrary client paths.
        old, _ = self.state(sid)
        state['media'], state['jpeg'] = old.get('media', {}), old.get('jpeg', {})
        for item in items:
            kind = item.get('kind')
            if kind == 'photo' and item.get('path') not in selected:
                raise ValueError('Фото сборки должно быть выбрано')
            if kind == 'time' and item.get('source') not in paths:
                raise ValueError('Слайду времени нужен исходный кадр')
            if kind in ('video', 'audio') and state['media'].get(item.get('path'), {}).get('kind') != kind:
                raise ValueError('Медиафайл не подключён')
            if kind == 'text' and len(str(item.get('text', ''))) > 3000:
                raise ValueError('Текст слишком длинный')
            if kind not in ('photo', 'time', 'text', 'audio', 'video') or not item.get('id'):
                raise ValueError('Неверный элемент сборки')
        with self.e.lock, self.e.conn() as c:
            r = c.execute('SELECT revision FROM unit_projects WHERE session_id=?', (sid,)).fetchone()
            actual = r['revision'] if r else 0
            if int(revision) != actual:
                raise ValueError('Проект изменился в другой вкладке. Обновите страницу перед сохранением.')
            c.execute('INSERT OR REPLACE INTO unit_projects VALUES(?,?,?)', (sid, actual + 1, json.dumps(state, ensure_ascii=False)))
        return {'revision': actual + 1}

    def _registered_update(self, sid, callback):
        with self.e.lock:
            state, rev = self.state(sid)
            callback(state)
            with self.e.conn() as c:
                c.execute('INSERT OR REPLACE INTO unit_projects VALUES(?,?,?)', (sid, rev + 1, json.dumps(state, ensure_ascii=False)))
        return {'revision': rev + 1}

    def connect_jpeg(self, sid, folder):
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('Папка JPEG не найдена')
        _, files = self.rows(sid)
        bystem = {}
        for p in root.rglob('*'):
            if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg'):
                bystem.setdefault(p.stem.casefold(), []).append(p)
        counts = Counter(Path(f['path']).stem.casefold() for f in files)
        linked, ambiguous = {}, []
        for f in files:
            stem = Path(f['path']).stem.casefold()
            candidates = bystem.get(stem, [])
            if len(candidates) == 1 and counts[stem] == 1:
                p = candidates[0]
                if p.resolve() == Path(f['path']).resolve():
                    continue
                linked[f['path']] = self.file_record(p)
            elif candidates:
                ambiguous.append(f['name'])
        result = self._registered_update(sid, lambda state: state.update(jpeg=linked))
        return {**result, 'linked': len(linked), 'ambiguous': ambiguous}

    @staticmethod
    def file_record(p):
        st = p.stat()
        return {'path': str(p), 'size': st.st_size, 'mtime': st.st_mtime_ns}

    def checked(self, record, sid=None):
        p = Path(self.e.offline.resolve(sid,record['path']) if sid else record['path'])
        if not p.is_file() or p.stat().st_size != record['size'] or p.stat().st_mtime_ns != record['mtime']:
            raise ValueError('Файл исчез или изменился: ' + p.name + '. Подключите папку заново.')
        return p

    def add_media(self, sid, path):
        p = Path(path).expanduser().resolve()
        if not p.is_file() or p.suffix.lower() not in MEDIA:
            raise ValueError('Поддерживаются MP4/MOV/WebM и WAV/MP3/M4A/OGG/FLAC')
        record = {**self.file_record(p), 'kind': MEDIA[p.suffix.lower()]}
        def update(state):
            state['media'][str(p)] = record
            state['items'].append({'id': uuid.uuid4().hex, 'kind': record['kind'], 'path': str(p)})
        return self._registered_update(sid, update)

    def people(self, sid, threshold):
        with self.e.conn() as c:
            r = c.execute('SELECT COUNT(*) n,COALESCE(SUM(n),0) detections FROM faces WHERE session_id=?', (sid,)).fetchone()
        job = self.e.jobs.get(sid + ':faces', {})
        key = (r['n'], r['detections'], threshold, bool(job.get('finished')), job.get('done'))
        if self.cache.get(sid, (None,))[0] == key:
            return self.cache[sid][1]
        session, _ = self.rows(sid)
        groups = cluster_faces(self.e._faces(sid), threshold, min_height=.025 if session['company']=='unitlab' else .07,
                               min_score=.70 if session['company']=='unitlab' else .80)
        with self.e.conn() as c:
            total=c.execute('SELECT COUNT(*) FROM files WHERE session_id=?',(sid,)).fetchone()[0]
        out = {'analyzed': r['n'], 'total': total, 'detections': r['detections'],
               'unique_estimate': len(groups) if groups or (r['n'] and not r['detections']) else None,
               'reliable_observations': sum(g['appearances'] for g in groups),
               'uncertain_observations': r['detections'] - sum(g['appearances'] for g in groups),
               'repeated': sum(g['appearances'] >= 2 for g in groups),
               'singletons': sum(g['appearances'] == 1 for g in groups), 'groups': groups,
               'running': bool(job and not job.get('finished')), 'error': job.get('error'),
               'complete': r['n'] == total and bool(total), 'threshold': threshold}
        self.cache[sid] = (key, out)
        return out

    def metrics_path(self, sid):
        return self.e.db_path.parent / 'unit-metrics' / (sid + '.json')

    def metrics(self, sid):
        p = self.metrics_path(sid)
        return json.loads(p.read_text('utf-8')) if p.exists() else None

    def analyze(self, sid):
        self.rows(sid)
        jid = sid + ':unit-metrics'
        if self.e.jobs.get(jid, {}).get('finished') is False:
            return {'job': jid}
        job = {'stage': 'Цвет и density всей съёмки', 'done': 0, 'total': 0, 'finished': False}
        self.e.jobs[jid] = job
        def run():
            from PIL import Image
            try:
                ss, files = self.rows(sid)
                job['total'] = len(files)
                density, colors, missing = Counter(), Counter(), []
                per_frame = []
                for f in files:
                    t = datetime.fromisoformat(f['capture_at'])
                    bucket = t.replace(minute=t.minute // 10 * 10, second=0).isoformat(' ')
                    density[bucket] += 1
                    data = self.e.preview_bytes(sid, f['path'], 240)
                    if data:
                        im = Image.open(io.BytesIO(data)).convert('RGB').resize((48, 48))
                        pal = im.quantize(colors=6).convert('RGB')
                        color = max(pal.getcolors(2304), key=lambda x: x[0])[1]
                        color = tuple(min(255, round(v / 32) * 32) for v in color)
                        colors[color] += 1
                        hist = im.convert('L').histogram()
                        per_frame.append({'path': f['path'], 'capture_at': f['capture_at'], 'dominant': color, 'histogram': hist})
                    else:
                        missing.append(f['name'])
                    job['done'] += 1
                if density:
                    current=datetime.fromisoformat(min(density));end=datetime.fromisoformat(max(density))
                    while current<=end:
                        density.setdefault(current.isoformat(' '),0)
                        current+=timedelta(minutes=10)
                out = {'session_id': sid, 'first': ss['first_capture'], 'last': ss['last_capture'],
                       'frames': len(files), 'analyzed_previews': len(per_frame), 'missing': missing,
                       'density': sorted(density.items()), 'colors': [{'rgb': col, 'frames': n} for col, n in colors.most_common(12)],
                       'per_frame': per_frame, 'basis': 'Встроенные/парные превью всех RAW; не проявка сенсорных данных',
                       'generated_at': datetime.now().isoformat()}
                p = self.metrics_path(sid)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(out, ensure_ascii=False), 'utf-8'); tmp.replace(p)
            except Exception as error:
                job['error'] = str(error)
            finally:
                job['finished'] = True
        threading.Thread(target=run, daemon=True).start()
        return {'job': jid}

    def project(self, sid):
        ss, files = self.rows(sid)
        state, rev = self.state(sid)
        metrics = self.metrics(sid)
        if metrics:
            metrics = {k: v for k, v in metrics.items() if k != 'per_frame'}
        return {'session': ss, 'files': [{k: f[k] for k in ('path','name','capture_at','kind','flags','brightness','sharpness','preview','analyzed')} for f in files],
                'state': state, 'revision': rev, 'people': self.people(sid, float(state.get('threshold', .45))), 'metrics': metrics,
                'metrics_job': self.e.jobs.get(sid + ':unit-metrics'),
                'channels': {'unit':'UNIT · мультилеерный репортаж'} if ss['company']=='unitlab' else CHANNELS}

    def summary(self, channel=None):
        with self.e.conn() as c:
            sessions = [dict(r) for r in c.execute('SELECT id,title,company,first_capture,last_capture FROM sessions ORDER BY first_capture DESC')]
            projects = {r['session_id']: json.loads(r['data']) for r in c.execute('SELECT * FROM unit_projects')}
            counts = {r['session_id']: dict(r) for r in c.execute('SELECT session_id,COUNT(*) frames,SUM(kind=\'raw\') raw FROM files GROUP BY session_id')}
        out = []
        for s in sessions:
            p = projects.get(s['id'], {})
            ch = p.get('channel') or s['company'] or 'other'
            if channel and channel != ch:
                continue
            faces=self.people(s['id'],float(p.get('threshold',.45)))
            duration=None
            if s['first_capture'] and s['last_capture']:
                duration=(datetime.fromisoformat(s['last_capture'])-datetime.fromisoformat(s['first_capture'])).total_seconds()
            out.append({**s, **counts.get(s['id'], {'frames':0,'raw':0}), 'channel':ch,
                        'face_detections':faces['detections'],'unique_people_estimate':faces['unique_estimate'],
                        'faces_complete':faces['complete'],'duration_seconds':duration,
                        'selected_unit':len(p.get('selected', [])) if p else None})
        return {'sessions':out, 'channels':CHANNELS, 'frames':sum(s['frames'] for s in out),
                'uls_status':'not_connected', 'note':'Общая статистика локального каталога. Синхронизация ULS ещё не подключена.'}

    def bridge(self):
        data = {**self.summary(), 'schema':'lumio.uls.statistics.alpha01', 'generated_at':datetime.now().isoformat(),
                'delivery_status':'pending', 'people_note':'Уникальные люди считаются внутри съёмки; между съёмками не суммируются.'}
        p = self.e.db_path.parent / 'uls-outbox' / 'LUMIO-statistics.json'
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8');tmp.replace(p)
        return {'path':str(p),'status':'pending','message':'Файл подготовлен локально; получение ULS не подтверждено'}

    def export(self, sid, output, draft=False):
        from PIL import Image, ImageDraw, ImageFont
        ss, files = self.rows(sid)
        state, rev = self.state(sid)
        bypath = {f['path']:f for f in files}
        if not state['items']:
            raise ValueError('Сборка пустая')
        if not str(output).strip():
            raise ValueError('Укажите папку экспорта')
        parent=Path(output).expanduser().resolve()
        sources=[Path(ss['source']).resolve()] + [Path(x['path']).resolve().parent for x in state['media'].values()] + [Path(x['path']).resolve().parent for x in state['jpeg'].values()]
        if any(parent == p or p in parent.parents or parent in p.parents for p in sources):
            raise ValueError('Папка экспорта должна быть отдельно от исходников, JPEG и медиа')
        plan=[]
        for i,item in enumerate(state['items'],1):
            kind=item['kind'];src=None;payload=None
            if kind=='photo':
                f=bypath[item['path']]
                if item['path'] in state['jpeg']:
                    src=self.checked(state['jpeg'][item['path']],sid)
                elif f['kind']!='raw':
                    src=Path(self.e.offline.resolve(sid,f['path']))
                    if not self.e.offline.available(sid,f['path'],fresh=True):
                        raise ValueError('Исходник изменился: '+f['name'])
                elif draft:
                    payload=self.e.preview_bytes(sid,item['path'],2048)
                    if not payload:raise ValueError('Нет превью: '+f['name'])
                else:raise ValueError('Нет обработанного JPEG: '+f['name']+'. Подключите JPEG или выберите черновик.')
                ext='.jpg'
            elif kind in ('audio','video'):
                src=self.checked(state['media'][item['path']],sid);ext=src.suffix.lower()
            else:
                ext='.jpg'
                payload=(bypath[item['source']]['capture_at'][11:19] if kind=='time' else item.get('text',''))
            plan.append((item,src,payload,f'{i:03d}{ext}'))
        # New isolated output every time. No stale files or overwritten earlier deliveries.
        parent.mkdir(parents=True,exist_ok=True)
        key=f'UNIT-{sid}-r{rev}-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
        stage=parent/('.'+key+'.partial');stage.mkdir()
        manifest=[]
        try:
            for item,src,payload,name in plan:
                dest=stage/name
                if item['kind'] in ('time','text'):
                    im=Image.new('RGB',(2000,3000),'#f7f7f5');d=ImageDraw.Draw(im)
                    fontpath=Path('C:/Windows/Fonts/segoeui.ttf')
                    text=str(payload)
                    import textwrap
                    text='\n'.join('\n'.join(textwrap.wrap(line,32) or ['']) for line in text.splitlines())
                    for size in range(90,13,-2):
                        font=ImageFont.truetype(str(fontpath),size)
                        bounds=d.multiline_textbbox((0,0),text,font=font,spacing=24)
                        if bounds[2]-bounds[0]<=1800 and bounds[3]-bounds[1]<=2700:break
                    d.multiline_text((1000,1500),text,font=font,fill='#191a1a',anchor='mm',align='center',spacing=24)
                    im.save(dest,'JPEG',quality=95)
                elif payload:
                    dest.write_bytes(payload)
                elif item['kind']=='photo' and ss['company']=='unitlab' and src.suffix.lower() in ('.jpg','.jpeg'):
                    shutil.copy2(src,dest)  # already rendered JPEG: preserve ICC and avoid another lossy encode
                elif item['kind']=='photo':
                    from PIL import ImageOps
                    im=ImageOps.exif_transpose(Image.open(src)).convert('RGB');im.save(dest,'JPEG',quality=95)
                else:shutil.copy2(src,dest)
                h=hashlib.sha256()
                with dest.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
                digest=h.hexdigest()
                manifest.append({'file':name,'kind':item['kind'],'source':Path(item.get('path') or item.get('source') or '').name if ss['company']=='unitlab' else item.get('path') or item.get('source'),
                                 'sha256':digest,'bytes':dest.stat().st_size})
            (stage/'MANIFEST.json').write_text(json.dumps({'session':sid,'revision':rev,'draft':bool(draft),'items':manifest},ensure_ascii=False,indent=2),'utf-8')
            metrics=self.metrics(sid)
            if metrics:(stage/'STATISTICS.json').write_text(json.dumps(metrics,ensure_ascii=False),'utf-8')
            target=parent/key;stage.rename(target)
            return {'path':str(target),'files':len(manifest),'draft':bool(draft),'revision':rev}
        except Exception:
            # Keep failed stage for diagnostics, never touch originals.
            raise

    def post(self, route, body):
        sid=body.get('session')
        if route=='save':return self.save(sid,body['state'],body['revision'])
        if route=='jpeg':return self.connect_jpeg(sid,body['folder'])
        if route=='media':return self.add_media(sid,body['path'])
        if route=='analyze':return self.analyze(sid)
        if route=='bridge':return self.bridge()
        if route=='export':
            jid=sid+':unit-export'
            if self.e.jobs.get(jid,{}).get('finished') is False:raise ValueError('Экспорт уже выполняется')
            job={'finished':False,'stage':'Экспорт сборки','done':0,'total':0};self.e.jobs[jid]=job
            def run():
                try:
                    job['result']=self.export(sid,body.get('output',''),bool(body.get('draft')))
                    if body.get('destination')=='disk':
                        from yadisk import YaDisk, file_md5
                        remote=str(body.get('remote','')).strip()
                        if not remote.startswith('disk:/') or '..' in remote.split('/') or remote=='disk:/':raise ValueError('Укажите отдельную папку disk:/…')
                        disk=YaDisk(self.e.disk_token('unit'))
                        root=Path(job['result']['path']);target=remote.rstrip('/')+'/'+root.name
                        disk.mkdirs(target);assets=list(root.iterdir());job['total']=len(assets)
                        for p in assets:
                            md5=file_md5(p);disk.upload(str(p),target+'/'+p.name,md5=md5)
                            have=disk.exists(target+'/'+p.name)
                            if not have or have.get('size')!=p.stat().st_size or have.get('md5')!=md5:raise ValueError('Не подтверждена загрузка '+p.name)
                            job['done']+=1
                        job['result']['remote']=target;job['result']['verified']=True
                except Exception as error:job['error']=str(error)
                finally:job['finished']=True
            threading.Thread(target=run,daemon=True).start();return {'job':jid}
        raise ValueError('Неизвестная команда UNIT')

    def serve_media(self, handler, query):
        import re
        import mimetypes
        state, _ = self.state(query.get('s', ''))
        record = state['media'].get(query.get('p', ''))
        if not record:
            raise ValueError('Медиафайл не подключён')
        path = self.checked(record,query.get('s'))
        size = path.stat().st_size
        start, end = 0, size - 1
        header = handler.headers.get('Range')
        if header:
            m = re.fullmatch(r'bytes=(\d+)-(\d*)', header)
            if not m:
                return handler.send(416, {'error':'Неверный диапазон'})
            start, end = int(m[1]), min(int(m[2]) if m[2] else end, end)
        if start > end:
            return handler.send(416, {'error':'Неверный диапазон'})
        handler.send_response(206 if header else 200)
        handler.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('Content-Length', str(end-start+1))
        if header:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        handler.end_headers()
        try:
            with path.open('rb') as stream:
                stream.seek(start)
                left = end-start+1
                while left:
                    chunk = stream.read(min(left, 65536))
                    if not chunk: break
                    handler.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
