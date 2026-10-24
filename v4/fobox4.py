#!/usr/bin/env python3
"""FOBOX 4.0 alpha.2 — local photo catalog, quality check, review and export.

Run:  py -3 fobox4.py            (opens http://127.0.0.1:8766 in the browser)
Optional: pip install pillow     (quality analysis, fast thumbnails)

Source photos are read-only for this program; export only makes copies.
"""
import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine import Engine

UI = Path(__file__).with_name('ui.html')


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
        types = [('CSV', '*.csv')] if kind == 'csv' else [('Все файлы', '*.*')]
        return filedialog.askopenfilename(filetypes=types) or ''
    finally:
        root.destroy()


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
                elif u.path == '/api/settings':
                    self.send(200, {'settings': engine.settings(), 'profile': engine.profile()})
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
                elif p == '/api/review':
                    out = engine.review(b['session'], b)
                elif p == '/api/meta':
                    out = engine.set_meta(b['session'], b)
                elif p == '/api/export':
                    out = engine.export(b['session'], b.get('output'), dry_run=bool(b.get('dry_run')))
                elif p == '/api/settings':
                    engine.save_settings(b.get('settings', {}))
                    if 'profile' in b:
                        engine.save_profile(b['profile'])
                    out = {'ok': True}
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
                        except Exception as e:
                            job['error'] = str(e)
                        job['finished'] = True
                    threading.Thread(target=run, daemon=True).start()
                    out = {'session': sid}
                elif p == '/api/pick':
                    out = {'path': pick_path(b.get('kind', 'dir'))}
                else:
                    return self.send(404, {'error': 'Не найдено'})
                self.send(200, out)
            except (ValueError, KeyError, OSError, json.JSONDecodeError) as e:
                self.send(400, {'error': str(e)})

        def log_message(self, *a):
            pass
    return Handler


def main():
    ap = argparse.ArgumentParser(description='FOBOX 4.0 alpha.2')
    ap.add_argument('--port', type=int, default=8766)
    ap.add_argument('--db', default=str(Path.home() / '.fobox' / 'fobox4.sqlite3'))
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    engine = Engine(Path(a.db).expanduser())
    server = ThreadingHTTPServer(('127.0.0.1', a.port), make_handler(engine))
    url = f'http://127.0.0.1:{a.port}/'
    print('FOBOX 4.0 alpha.2:', url, '| база:', a.db, '| Ctrl+C — остановить', flush=True)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
