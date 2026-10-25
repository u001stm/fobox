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
CODE = [Path(__file__), Path(__file__).with_name('engine.py'), Path(__file__).with_name('yadisk.py'), Path(__file__).with_name('faces.py')]


def build_id():
    """Changes whenever any program file changes: the open page reloads itself, the server restarts."""
    return '-'.join(str(int(p.stat().st_mtime)) for p in CODE + [UI] if p.exists())
APP_VERSION = '4.13.0'  # bump together with UI_VERSION in ui.html


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
                elif u.path == '/api/stats':
                    self.send(200, engine.stats())
                elif u.path == '/api/build':
                    self.send(200, {'build': build_id(), 'version': APP_VERSION})
                elif u.path == '/api/settings':
                    self.send(200, {'settings': engine.settings(), 'profile': engine.profile(), 'version': APP_VERSION, 'companies': engine.companies()})
                elif u.path == '/api/session':
                    self.send(200, engine.session(q.get('id', '')))
                elif u.path == '/api/job':
                    self.send(200, engine.jobs.get(q.get('id', ''), {'finished': True, 'error': 'Нет задачи'}))
                elif u.path == '/api/preview':
                    data = engine.preview_bytes(q.get('s', ''), q.get('p', ''), min(int(q.get('size', 480)), 2048))
                    if data is None:
                        self.send(404, {'error': 'Нет превью'})
                    else:
                        self.send(200, data, 'image/jpeg', cache=True)
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
                p = urlparse(self.path).path
                if p == '/api/import':
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
                elif p == '/api/rename':
                    out = engine.rename_person(b['session'], b['person'], b.get('name'), b.get('role'))
                elif p == '/api/set-common':
                    out = engine.set_common(b['session'], b['path'], bool(b.get('scene')))
                elif p == '/api/faces':
                    out = engine.faces_status()
                elif p == '/api/faces-install':
                    out = engine.install_faces()
                elif p == '/api/faces-run':
                    out = engine.start_faces(b['session'], bool(b.get('force')))
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
