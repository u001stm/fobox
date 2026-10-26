"""UNIT processing alpha. Separate environment, persistent jobs, managed originals."""
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from engine import RAW_EXT, IMG_EXT

ENV = 'unitlab'
DEFAULT = {'exposure': 0., 'wb': 'camera', 'warmth': 0., 'contrast': 0., 'long_edge': 5000}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def recipe(value):
    out = dict(DEFAULT)
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Неверные параметры обработки')
    out.update(value)
    for key, low, high in [('exposure', -3, 3), ('warmth', -50, 50), ('contrast', -40, 40)]:
        out[key] = float(out[key])
        if not math.isfinite(out[key]) or not low <= out[key] <= high:
            raise ValueError('Параметр вне диапазона: ' + key)
    if out['wb'] not in ('camera', 'auto') or out['long_edge'] not in (2000, 3000, 5000, 6000):
        raise ValueError('Неверный WB или размер')
    return out


class LabWorkspace:
    def __init__(self, engine, unit, autostart=True):
        self.e, self.unit = engine, unit
        self.root = engine.db_path.parent / 'unit-processing'
        self.root.mkdir(parents=True, exist_ok=True)
        self.pending = queue.Queue()
        self.runtime = Path(os.environ.get('LUMIO_LAB_PYTHON', str(Path(__file__).parent / '.lab-runtime' / 'Scripts' / 'python.exe')))
        with engine.conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS lab_projects(session_id TEXT PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lab_assets(session_id TEXT,sha256 TEXT,path TEXT,bytes INTEGER,
               PRIMARY KEY(session_id,sha256));
            CREATE TABLE IF NOT EXISTS lab_sources(session_id TEXT,source TEXT,sha256 TEXT,verified REAL,
               PRIMARY KEY(session_id,source,sha256));
            CREATE TABLE IF NOT EXISTS lab_renders(session_id TEXT,path TEXT,recipe TEXT,render_key TEXT,jpeg TEXT,preview TEXT,sha256 TEXT,
               PRIMARY KEY(session_id,path));
            CREATE TABLE IF NOT EXISTS lab_jobs(id TEXT PRIMARY KEY,session_id TEXT,kind TEXT,payload TEXT,status TEXT,
               done INTEGER DEFAULT 0,total INTEGER DEFAULT 0,stage TEXT,error TEXT,result TEXT,created REAL,updated REAL);
            ''')
            cfg = {'layout': 'unitlab', 'short': 'U', 'color': '#555555',
                   'note': 'Проверяемый импорт, отбор, RAW-коррекция и сборка'}
            c.execute('INSERT OR IGNORE INTO companies(id,name,config,sort) VALUES(?,?,?,?)',
                      (ENV, 'UNIT · обработка α01', json.dumps(cfg, ensure_ascii=False), 11))
            interrupted = c.execute("SELECT id FROM lab_jobs WHERE status IN ('queued','running') ORDER BY created").fetchall()
            c.execute("UPDATE lab_jobs SET status='queued',stage='Продолжение после перезапуска' WHERE status='running'")
        for row in interrupted:
            self.pending.put(row['id'])
        from ml_service import MultiLayer
        self.multilayer = MultiLayer(self)
        if autostart:
            threading.Thread(target=self.worker, daemon=True).start()

    def validate(self, sid):
        session, files = self.unit.rows(sid)
        if session['company'] != ENV:
            raise ValueError('Команда доступна только в UNIT · обработка')
        return session, files

    def update(self, jid, **fields):
        fields['updated'] = time.time()
        with self.e.conn() as c:
            c.execute('UPDATE lab_jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), jid))

    def job(self, jid):
        with self.e.conn() as c:
            row = c.execute('SELECT * FROM lab_jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            raise ValueError('Задача не найдена')
        d = dict(row)
        return {k: d[k] for k in ('id', 'session_id', 'kind', 'status', 'done', 'total', 'stage', 'error')} | {
            'finished': d['status'] in ('complete', 'failed', 'cancelled'), 'result': json.loads(d['result'] or '{}')}

    def enqueue(self, sid, kind, payload):
        jid = uuid.uuid4().hex
        with self.e.lock, self.e.conn() as c:
            if c.execute("SELECT 1 FROM lab_jobs WHERE session_id=? AND status IN ('queued','running')", (sid,)).fetchone():
                raise ValueError('Для этой съёмки уже выполняется задача')
            c.execute('INSERT INTO lab_jobs(id,session_id,kind,payload,status,stage,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                      (jid, sid, kind, json.dumps(payload), 'queued', 'В очереди', time.time(), time.time()))
        self.pending.put(jid)
        return {'job': jid, 'session': sid}

    def start_import(self, body):
        source = Path(str(body.get('source', ''))).expanduser().resolve()
        if not body.get('source') or not source.is_dir():
            raise ValueError('Выберите папку исходников')
        sid = body.get('session')
        if sid:
            self.validate(sid)
        else:
            sid = uuid.uuid4().hex[:12]
        if source == self.root or self.root in source.parents or source in self.root.parents:
            raise ValueError('Исходники должны лежать отдельно от каталога приложения')
        title = str(body.get('title', '')).strip()[:180] or source.name
        with self.e.conn() as c:
            c.execute('INSERT OR IGNORE INTO lab_projects VALUES(?,?)', (sid, title))
        return self.enqueue(sid, 'import', {'source': str(source), 'title': title})

    def worker(self):
        while True:
            jid = self.pending.get()
            try:
                self.run(jid)
            finally:
                self.pending.task_done()

    def run(self, jid):
        with self.e.conn() as c:
            row = dict(c.execute('SELECT * FROM lab_jobs WHERE id=?', (jid,)).fetchone())
        if row['status'] not in ('queued', 'running'):
            return
        self.update(jid, status='running', error=None)
        try:
            payload = json.loads(row['payload'])
            if row['kind'] == 'import':
                result = self.import_files(row['session_id'], payload, jid)
            elif row['kind'] == 'render':
                result = self.render_files(row['session_id'], payload, jid)
            elif row['kind'] == 'faces':
                result = self.run_faces(row['session_id'], payload, jid)
            elif row['kind'].startswith('ml-'):
                result = self.multilayer.run(row['kind'][3:],row['session_id'],payload,jid)
            else:
                raise ValueError('Неизвестная задача')
            self.update(jid, status='complete', stage='Готово', result=json.dumps(result))
        except Exception as exc:
            from ml_metadata import Cancelled
            self.update(jid, status='cancelled' if isinstance(exc,Cancelled) else 'failed', stage='Отменено' if isinstance(exc,Cancelled) else 'Задача остановлена', error=str(exc))

    def import_files(self, sid, payload, jid):
        source = Path(payload['source'])
        files = sorted(p for p in source.rglob('*') if p.is_file() and p.suffix.lower() in RAW_EXT | IMG_EXT and not p.name.startswith('._'))
        if not files:
            raise ValueError('В папке нет RAW или JPEG')
        archive = self.root / sid / 'originals'
        copied = duplicates = 0
        for index, src in enumerate(files):
            self.update(jid, stage='Копирую и проверяю · ' + src.name, done=index, total=len(files))
            stat = src.stat()
            digest = sha256(src)
            with self.e.conn() as c:
                have = c.execute('SELECT path FROM lab_assets WHERE session_id=? AND sha256=?', (sid, digest)).fetchone()
            dest = Path(have['path']) if have else archive / digest[:2] / digest / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if sha256(dest) != digest:
                    raise ValueError('Повреждена локальная копия: ' + src.name)
                duplicates += 1
            else:
                if shutil.disk_usage(dest.parent).free < stat.st_size + 20*1024*1024:
                    raise ValueError('Недостаточно места для копии '+src.name)
                temp = dest.with_name(dest.name + '.partial')
                with src.open('rb') as inp, temp.open('wb') as out:
                    shutil.copyfileobj(inp, out, 1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
                if sha256(temp) != digest:
                    raise ValueError('Не совпал SHA-256: ' + src.name)
                after = src.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('Исходник изменился: ' + src.name)
                shutil.copystat(src, temp)
                temp.replace(dest)
                copied += 1
            with self.e.conn() as c:
                c.execute('INSERT OR IGNORE INTO lab_assets VALUES(?,?,?,?)', (sid, digest, str(dest), stat.st_size))
                c.execute('INSERT OR REPLACE INTO lab_sources VALUES(?,?,?,?)', (sid, str(src), digest, time.time()))
        self.update(jid, stage='Каталог и превью', done=len(files))
        self.e.import_folder(archive, {'company': ENV, 'workspace': ENV, 'fixed_session_id': sid, 'title': payload['title']})
        self.e.offline.warm(sid)
        self.e.offline.dirty.add(sid)
        return {'session': sid, 'copied': copied, 'duplicates': duplicates}

    def project(self, sid):
        self.validate(sid)
        with self.e.conn() as c:
            renders = {r['path']: {'recipe': json.loads(r['recipe']), 'key': r['render_key']} for r in c.execute('SELECT * FROM lab_renders WHERE session_id=?', (sid,))}
            jobs = [self.job(r['id']) for r in c.execute('SELECT id FROM lab_jobs WHERE session_id=? ORDER BY created DESC LIMIT 8', (sid,))]
            sources = c.execute('SELECT count(DISTINCT source) FROM lab_sources WHERE session_id=?', (sid,)).fetchone()[0]
        return {'renders': renders, 'jobs': jobs, 'sources_verified': sources, 'runtime_ready': self.runtime.is_file(), 'defaults': DEFAULT}

    def start_render(self, body):
        sid = body['session']
        _, files = self.validate(sid)
        selected = body.get('paths', [])
        state, _ = self.unit.state(sid)
        if not selected or len(selected) != len(set(selected)) or not set(selected) <= set(state['selected']):
            raise ValueError('Обрабатывать можно только отобранные кадры')
        if not self.runtime.is_file():
            raise ValueError('RAW-движок ещё не установлен')
        return self.enqueue(sid, 'render', {'paths': selected, 'recipe': recipe(body.get('recipe', {}))})

    def render_files(self, sid, payload, jid):
        _, files = self.validate(sid)
        bypath = {f['path']: f for f in files}
        settings = recipe(payload['recipe'])
        for index, path in enumerate(payload['paths']):
            f = bypath[path]
            source = self.e.offline.resolve(sid, path)
            if not source or not Path(source).is_file():
                raise ValueError('Исходник недоступен: ' + f['name'])
            self.update(jid, stage='Проявка · ' + f['name'], done=index, total=len(payload['paths']))
            source_hash = sha256(source)
            key = hashlib.sha256((source_hash + json.dumps(settings, sort_keys=True) + '|raw-worker-v001').encode()).hexdigest()
            folder = self.root / sid / 'renders' / key
            final = folder / 'final.jpg'
            if not final.is_file() or not (folder / 'recipe.json').is_file():
                process = subprocess.run([str(self.runtime), str(Path(__file__).with_name('lab_render.py'))],
                    input=json.dumps({'source': str(source), 'raw': f['kind'] == 'raw', 'target': str(folder), 'recipe': settings}),
                    text=True, encoding='utf-8', capture_output=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                if process.returncode:
                    raise ValueError((process.stderr or process.stdout)[-1500:])
            if sha256(source) != source_hash:
                raise ValueError('Исходник изменился во время проявки')
            from PIL import Image
            with Image.open(final) as image:
                image.verify()
            with self.e.lock, self.e.conn() as c:
                c.execute('INSERT OR REPLACE INTO lab_renders VALUES(?,?,?,?,?,?,?)',
                    (sid, path, json.dumps(settings), key, str(final), str(folder / 'preview.jpg'), sha256(final)))
                row = c.execute('SELECT data,revision FROM unit_projects WHERE session_id=?', (sid,)).fetchone()
                state = json.loads(row['data'])
                st = final.stat()
                state.setdefault('jpeg', {})[path] = self.unit.file_record(final)
                c.execute('UPDATE unit_projects SET data=?,revision=revision+1 WHERE session_id=?', (json.dumps(state), sid))
            self.update(jid, done=index + 1)
        self.e.offline.dirty.add(sid)
        return {'rendered': len(payload['paths']), 'long_edge': settings['long_edge']}

    def preview(self, sid, path, expected_key):
        self.validate(sid)
        with self.e.conn() as c:
            row = c.execute('SELECT * FROM lab_renders WHERE session_id=? AND path=?', (sid, path)).fetchone()
        if not row or row['render_key'] != expected_key:
            raise ValueError('Обработанное превью не найдено')
        return Path(row['preview']).read_bytes()

    def start_faces(self, body):
        self.validate(body['session'])
        model = body.get('model','antelopev2')
        if model not in ('buffalo_l','antelopev2'):
            raise ValueError('Неизвестная модель лиц')
        return self.enqueue(body['session'],'faces',{'model':model})

    def run_faces(self, sid, payload, jid):
        model=payload['model']
        if model=='antelopev2':
            directory=Path.home()/'.insightface'/'models'/'antelopev2'
            names=('scrfd_10g_bnkps.onnx','glintr100.onnx','2d106det.onnx','genderage.onnx')
            if not all((directory/n).is_file() for n in names):
                import urllib.request
                import zipfile
                self.update(jid,stage='Загружаю усиленную модель · один раз, около 344 МБ')
                directory.mkdir(parents=True,exist_ok=True)
                archive=directory/'download.partial'
                with urllib.request.urlopen('https://github.com/deepinsight/insightface/releases/download/model-zoo/antelopev2.zip',timeout=120) as response, archive.open('wb') as out:
                    shutil.copyfileobj(response,out,1024*1024)
                if sha256(archive)!='8e182f14fc6e80b3bfa375b33eb6cff7ee05d8ef7633e738d1c89021dcf0c5c5':
                    raise ValueError('Контрольная сумма модели не совпала')
                with zipfile.ZipFile(archive) as z:
                    for name in names:
                        member=next((n for n in z.namelist() if Path(n).name==name),None)
                        if not member:raise ValueError('В пакете нет '+name)
                        temp=directory/(name+'.partial')
                        with z.open(member) as src,temp.open('wb') as out:shutil.copyfileobj(src,out)
                        temp.replace(directory/name)
                archive.unlink()
        self.unit.cache.pop(sid,None)
        job=self.e.start_faces(sid,True,model_pack=model)['job']
        while not self.e.jobs[job].get('finished'):
            item=self.e.jobs[job]
            self.update(jid,stage='Лица · '+model,done=item.get('done',0),total=item.get('total',0))
            time.sleep(.5)
        if self.e.jobs[job].get('error'):raise ValueError(self.e.jobs[job]['error'])
        with self.e.conn() as c:
            row=c.execute('SELECT meta FROM sessions WHERE id=?',(sid,)).fetchone()
            meta=json.loads(row['meta'] or '{}');meta['lab_face_model']=model
            c.execute('UPDATE sessions SET meta=? WHERE id=?',(json.dumps(meta),sid))
        self.unit.cache.pop(sid,None)
        return {'model':model,'analyzed':self.e.jobs[job].get('done',0)}
