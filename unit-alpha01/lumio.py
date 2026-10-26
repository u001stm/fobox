#!/usr/bin/env python3
"""LUMIO — local photo catalog, quality check, automatic kits, review and export.

Run:  py -3 lumio.py            (opens http://127.0.0.1:8766 in the browser)
Optional: pip install pillow     (quality analysis, fast thumbnails)

Source photos are read-only for this program; export only makes copies.
"""
import argparse
import json
import os
import shutil
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine import Engine

UI = Path(__file__).with_name('ui.html')
CODE = [Path(__file__).with_name('offline_catalog.py'), Path(__file__).with_name('offline.js'), Path(__file__).with_name('unit_workspace.py'), Path(__file__).with_name('unit.js'), Path(__file__).with_name('unit.css'), Path(__file__), Path(__file__).with_name('engine.py'), Path(__file__).with_name('yadisk.py'), Path(__file__).with_name('faces.py')]
CODE += [Path(__file__).with_name(n) for n in ('lab_workspace.py','lab_render.py','lab.js','lab.css')]
CODE += [Path(__file__).with_name(n) for n in ('ml_core.py','ml_metadata.py','ml_service.py','ml_contracts.py','ml_schema.json','ml_outputs.py','ml.js','ml.css')]


def build_id():
    """Changes whenever any program file changes: the open page reloads itself, the server restarts."""
    return '-'.join(str(int(p.stat().st_mtime)) for p in CODE + [UI] if p.exists())
APP_VERSION = '4.23.0-multilayer-alpha01'


def pick_path(kind):
    """Native folder/file dialog via tkinter (bundled with Python on Windows)."""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        raise ValueError('Окно выбора недоступно — вставьте путь вручную')
    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        if kind == 'dir':
            return filedialog.askdirectory() or ''
        types = [('Ведомость Excel или CSV', '*.xlsx *.xlsm *.csv'), ('Все файлы', '*.*')] if kind == 'csv' else [('Все файлы', '*.*')]
        return filedialog.askopenfilename(filetypes=types) or ''
    finally:
        root.destroy()


def open_folder(engine, sid):
    """Show the last export of this session in Explorer/Finder (only that folder can be opened)."""
    import os
    import subprocess
    import sys
    st = engine.session(sid)['session']
    path = json.loads(st['meta'] or '{}').get('last_export')
    if not path or not Path(path).is_dir():
        raise ValueError('Папка экспорта не найдена')
    if sys.platform.startswith('win'):
        os.startfile(path)  # noqa: S606 - local desktop app
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])
    return {'ok': True, 'path': path}


def make_handler(engine):
    from unit_workspace import UnitWorkspace
    unit = UnitWorkspace(engine)
    from lab_workspace import LabWorkspace
    lab = LabWorkspace(engine, unit)
    engine.offline.start()
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, mime='application/json; charset=utf-8', cache=False):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'max-age=86400' if cache else 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == '/':
                    self.send(200, UI.read_bytes(), 'text/html; charset=utf-8')
                elif u.path in ('/unit.js', '/unit.css', '/lab.js', '/lab.css', '/ml.js','/ml.css'):
                    self.send(200, UI.with_name(u.path[1:]).read_bytes(), 'application/javascript; charset=utf-8' if u.path.endswith('.js') else 'text/css; charset=utf-8')
                elif u.path == '/api/unit/project':
                    project = unit.project(q.get('id', ''))
                    if project['session']['company'] == 'unitlab':
                        project['lab'] = lab.project(q['id'])
                        project['multilayer'] = lab.multilayer.project(q['id'])
                    self.send(200, project)
                elif u.path == '/api/ml/project':
                    self.send(200,lab.multilayer.project(q.get('session','')))
                elif u.path == '/api/ml/media':
                    lab.multilayer.serve(self,q)
                elif u.path == '/api/ml/file':
                    import mimetypes
                    file=lab.multilayer.file(q.get('session',''),q.get('name',''))
                    self.send(200,file.read_bytes(),mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
                elif u.path == '/api/lab/job':
                    self.send(200, lab.job(q.get('id', '')))
                elif u.path == '/api/lab/jobs':
                    with engine.conn() as c:
                        jobs = [lab.job(r['id']) for r in c.execute('SELECT id FROM lab_jobs ORDER BY created DESC LIMIT 10')]
                    self.send(200, {'jobs':jobs})
                elif u.path == '/api/lab/preview':
                    self.send(200, lab.preview(q.get('s',''), q.get('p',''), q.get('v','')), 'image/jpeg')
                elif u.path == '/api/unit/summary':
                    self.send(200, unit.summary(q.get('channel')))
                elif u.path == '/api/unit/media':
                    unit.serve_media(self, q)
                elif u.path == '/api/media-state':
                    if q.get('ui'):
                        import time
                        engine.offline.ui_seen = time.time()
                    media_state = engine.offline.api_state(q.get('session'))
                    sid = q.get('session')
                    if sid:
                        with engine.conn() as connection:
                            ml_row = connection.execute('SELECT data FROM ml_projects WHERE session_id=?', (sid,)).fetchone()
                        if ml_row and media_state.get('project'):
                            sources = json.loads(ml_row['data'])['config']['sources']
                            media_state['project']['ml_sources'] = {'total': len(sources), 'online': sum(Path(s['root']).is_dir() for s in sources)}
                    self.send(200, media_state)
                elif u.path == '/offline.js':
                    self.send(200, UI.with_name('offline.js').read_bytes(), 'application/javascript; charset=utf-8')
                elif u.path == '/api/stats':
                    self.send(200, engine.stats())
                elif u.path == '/api/build':
                    self.send(200, {'build': build_id(), 'version': APP_VERSION})
                elif u.path == '/api/settings':
                    j = engine.jobs.get('faces:install')
                    self.send(200, {'settings': engine.settings(), 'profile': engine.profile(), 'version': APP_VERSION, 'companies': engine.companies(),
                                    'faces_install': ({k: j.get(k) for k in ('stage', 'done', 'total', 'error', 'finished')} if j else None)})
                elif u.path == '/api/session':
                    self.send(200, engine.session(q.get('id', '')))
                elif u.path == '/api/job':
                    self.send(200, engine.jobs.get(q.get('id', ''), {'finished': True, 'error': 'Нет задачи'}))
                elif u.path == '/api/preview':
                    data = engine.preview_bytes(q.get('s', ''), q.get('p', ''), min(int(q.get('size', 480)), 2048))
                    if data is None:
                        self.send(404, {'error': 'Нет превью'})
                    else:
                        self.send(200, data, 'image/jpeg', cache=False)
                else:
                    self.send(404, {'error': 'Не найдено'})
            except (ValueError, OSError) as e:
                self.send(400, {'error': str(e)})

        def do_POST(self):
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if n > 2_000_000:
                    raise ValueError('Слишком большой запрос')
                b = json.loads(self.rfile.read(n) or b'{}')
                if b.get('session') and urlparse(self.path).path != '/api/delete' and not urlparse(self.path).path.startswith('/api/unit/'):
                    b['session'] = engine.active_session_id(b['session'])
                p = urlparse(self.path).path
                if p == '/api/ml/create':
                    out=lab.multilayer.create(b.get('title',''))
                elif p == '/api/ml/cancel':
                    out=lab.multilayer.cancel(b['job'])
                elif p == '/api/ml/open':
                    result=lab.multilayer.latest(b['session'])
                    if not result:raise ValueError('Нет готового отчёта')
                    path=Path(result['folder'])
                    if b.get('kind') in ('working','final'):
                        root=lab.multilayer.folder(b['session'])/('WORK' if b['kind']=='working' else 'EXPORTS')
                        folders=sorted(p for p in root.glob('v*') if p.is_dir())
                        if not folders:raise ValueError('Сначала создайте рабочую последовательность')
                        path=folders[-1]/'TIMELINE' if b['kind']=='working' else folders[-1]/'CLIENT'/'MEDIA'
                    if os.name=='nt':os.startfile(str(path))
                    out={'path':str(path)}
                elif p.startswith('/api/ml/'):
                    out=lab.multilayer.post(p.rsplit('/',1)[-1],b)
                elif p == '/api/lab/import':
                    out = lab.start_import(b)
                elif p == '/api/lab/render':
                    out = lab.start_render(b)
                elif p == '/api/lab/faces':
                    out = lab.start_faces(b)
                elif p == '/api/lab/retry':
                    with engine.conn() as c:
                        row = c.execute('SELECT * FROM lab_jobs WHERE id=?', (b.get('job',''),)).fetchone()
                    if not row or row['status'] != 'failed':
                        raise ValueError('Нет остановленной задачи для повтора')
                    out = lab.enqueue(row['session_id'],row['kind'],json.loads(row['payload']))
                elif p == '/api/card-autostart':
                    from card_watch import configure
                    out = configure(engine.db_path, UI.parent, bool(b.get('enabled')), self.server.server_address[1])
                elif p == '/api/relink':
                    out = engine.offline.relink(b['session'], b.get('source',''))
                elif p == '/api/offline-prepare':
                    engine.offline.warm(b['session'])
                    out = {'ok':True}
                elif p == '/api/card-passport':
                    out = engine.offline.passport(b['session'])
                elif p == '/api/unit/import':
                    b.update(company='unit', workspace='unit', students=None, teachers=None)
                    out = engine.start_import(b)
                elif p == '/api/unit/faces-run':
                    unit.rows(b['session'])
                    unit.cache.pop(b['session'], None)
                    out = engine.start_faces(b['session'], bool(b.get('force')))
                elif p.startswith('/api/unit/'):
                    out = unit.post(p.rsplit('/',1)[-1], b)
                elif p == '/api/import':
                    out = engine.start_import(b)
                elif p == '/api/review-beacon':   # last unsaved change when the tab closes
                    b['approved'] = False
                    out = engine.review(b['session'], b)
                elif p == '/api/review':
                    out = engine.review(b['session'], b)
                elif p == '/api/company':
                    out = engine.delete_company(b['delete']) if b.get('delete') else engine.save_company(b)
                elif p == '/api/set-company':
                    out = engine.set_company(b['session'], b['company'])
                elif p == '/api/category':
                    out = engine.set_category(b['session'], b['section'], b['cat'])
                elif p == '/api/divider':
                    out = engine.set_divider(b['session'], b['path'], b.get('cat'))
                elif p == '/api/upload':
                    out = engine.start_upload(b['session'])
                elif p == '/api/disk-target':
                    out = {'target': engine.disk_target(b['session'])}
                elif p == '/api/disk-check':
                    from yadisk import YaDisk
                    out = YaDisk(b.get('token') or engine.disk_token(b.get('company') or 'crous')).info()
                elif p == '/api/rebuild':
                    out = engine.rebuild(b['session'], b.get('sections'))
                elif p == '/api/role':
                    out = engine.set_role(b['session'], b)
                elif p == '/api/meta':
                    out = engine.set_meta(b['session'], b)
                elif p == '/api/export':
                    if b.get('dry_run'):
                        out = engine.export(b['session'], b.get('output'), dry_run=True)
                    else:
                        out = engine.start_export(b['session'], b.get('output'))
                elif p == '/api/open':
                    out = open_folder(engine, b['session'])
                elif p == '/api/settings':
                    engine.save_settings(b.get('settings', {}))
                    if 'profile' in b:
                        engine.save_profile(b['profile'])
                    out = {'ok': True}
                elif p == '/api/add-teacher':
                    out = engine.add_teacher(b['session'], b.get('name'), b.get('frame'), b.get('role', ''))
                elif p == '/api/rename':
                    out = engine.rename_person(b['session'], b['person'], b.get('name'), b.get('role'))
                elif p == '/api/scene-boundary':
                    out = engine.set_scene_boundary(b['session'],b['path'],b.get('split'))
                elif p == '/api/set-common':
                    out = engine.set_common(b['session'], b['path'], bool(b.get('scene')))
                elif p == '/api/faces':
                    out = engine.faces_status()
                elif p == '/api/faces-install':
                    out = engine.install_faces()
                elif p == '/api/faces-run':
                    out = engine.start_faces(b['session'], bool(b.get('force')), bool(b.get('rebuild')))
                elif p == '/api/not-defect':
                    out = engine.not_defect(b['session'], b['path'], b.get('ok', True) is not False)
                elif p == '/api/reason':
                    out = engine.set_reason(b['session'], b['section'], b['path'], b['reason'])
                elif p == '/api/reset':
                    out = engine.reset_session(b['session'])
                elif p == '/api/delete':
                    engine.delete_session(b['session'])
                    out = {'ok': True}
                elif p == '/api/reanalyze':
                    sid = b['session']
                    job = {'session': sid, 'stage': 'Проверка качества', 'done': 0, 'total': 0, 'error': None, 'finished': False}
                    engine.jobs[sid] = job

                    def run():
                        try:
                            engine.analyze(sid, job)
                            engine.suggest(sid)
                            engine._faces_auto(sid)
                        except Exception as e:
                            job['error'] = str(e)
                        job['finished'] = True
                    threading.Thread(target=run, daemon=True).start()
                    out = {'session': sid}
                elif p == '/api/worksheet':
                    from engine import worksheet_summary
                    out = worksheet_summary(b.get('path', ''))
                elif p == '/api/pick':
                    out = {'path': pick_path(b.get('kind', 'dir'))}
                else:
                    return self.send(404, {'error': 'Команда не найдена — перезапустите программу (закройте окно и запустите «Запустить LUMIO.bat»)', 'code': 'no_route'})
                if b.get('session') and p != '/api/delete':
                    engine.offline.dirty.add(b['session'])
                self.send(200, out)
            except Exception as e:   # ValueError, OSError, DiskError … — the page shows the message
                self.send(400, {'error': str(e)})

        def log_message(self, *a):
            pass
    return Handler


class Server(ThreadingHTTPServer):
    # Windows lets a second program take a busy port when address reuse is on; then requests
    # keep going to the old copy. Exclusive bind makes a busy port an explicit error.
    allow_reuse_address = False
    daemon_threads = True


def running_version(port):
    """Version of a LUMIO already answering on this port: None = nothing there, '' = an old copy."""
    import urllib.request
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/settings', timeout=1.5) as r:
            return json.loads(r.read() or b'{}').get('version', '')
    except Exception as e:
        if getattr(e, 'code', None):      # something answers with an HTTP error: an old copy or another app
            return ''
        return None


def bind(port):
    """Start on `port`; if an old LUMIO (or anything else) holds it, use the next free port."""
    if os.environ.get('LUMIO_RESTART') or os.environ.get('FOBOX_RESTART'):
        # self-restart: our own port is released a moment after the old process closes it
        import time
        for _ in range(40):
            try:
                return Server(('127.0.0.1', port), None), port
            except OSError:
                time.sleep(0.25)
    for p in range(port, port + 20):
        ver = running_version(p)
        if ver == APP_VERSION:
            return None, p                # this version already runs: just open it
        if ver is not None:
            print(f'! На порту {p} работает старая копия LUMIO/LUMIO — её окно можно закрыть. '
                  'Эта версия запустится на следующем свободном порту.', flush=True)
            continue
        try:
            return Server(('127.0.0.1', p), None), p
        except OSError:
            continue
    raise SystemExit('Не удалось найти свободный порт 8766–8785')


DEFAULT_DB = Path.home() / '.lumio' / 'lumio.sqlite3'
OLD_DB = Path.home() / '.fobox' / 'fobox4.sqlite3'   # catalog of earlier versions


def migrate_catalog(db):
    """First start after the rename: copy the earlier catalog (the old file stays as a backup) and move the preview cache."""
    if db != DEFAULT_DB or db.exists() or not OLD_DB.exists():
        return
    import sqlite3
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(OLD_DB) as src, sqlite3.connect(db) as dst:
        src.backup(dst)
    old_prev = OLD_DB.parent / 'previews'
    if old_prev.is_dir() and not (db.parent / 'previews').exists():
        try:
            shutil.move(str(old_prev), str(db.parent / 'previews'))   # cache only: move instead of a slow copy
        except OSError:
            pass
    print('Каталог перенесён в', db)


def main():
    ap = argparse.ArgumentParser(description='LUMIO')
    ap.add_argument('--port', type=int, default=8766)
    ap.add_argument('--db', default=str(DEFAULT_DB))
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    migrate_catalog(Path(a.db))
    server, port = bind(a.port)
    url = f'http://127.0.0.1:{port}/'
    if server is None:
        print('LUMIO', APP_VERSION, 'уже запущен:', url, flush=True)
        if not a.no_browser:
            webbrowser.open(url)
        return
    engine = Engine(Path(a.db).expanduser())
    server.RequestHandlerClass = make_handler(engine)
    print(f'LUMIO {APP_VERSION}: {url} | база: {a.db} | Ctrl+C — остановить', flush=True)
    # face recognition is part of the normal flow: on first start it installs itself in the background
    try:
        if not engine.faces_status()['ready']:
            engine.install_faces()
            print('Устанавливаю распознавание лиц в фоне (один раз)…', flush=True)
    except Exception as e:
        print('Распознавание лиц:', e, flush=True)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    restart = threading.Event()

    def watch():
        # program files updated (by you or by Claude): restart this process with the new code
        start = {p: p.stat().st_mtime for p in CODE if p.exists()}
        while not restart.is_set():
            restart.wait(1.0)
            try:
                if any(p.exists() and p.stat().st_mtime != t for p, t in start.items()):
                    restart.wait(0.7)        # let the copy finish
                    print('Файлы программы обновились — перезапуск…', flush=True)
                    restart.set()
                    server.shutdown()
            except OSError:
                pass
    threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print('Остановлено.', flush=True)
        restart.clear()
    finally:
        server.server_close()
    if restart.is_set():
        args = [a2 for a2 in sys.argv[1:] if a2 != '--no-browser'] + ['--no-browser', '--port', str(port)]
        os.environ['LUMIO_RESTART'] = '1'
        os.execv(sys.executable, [sys.executable, os.path.abspath(sys.argv[0])] + args)


if __name__ == '__main__':
    main()
