#!/usr/bin/env python3
"""FOBOX 4.0 alpha: local read-only analytics foundation (stdlib only)."""
import argparse
import json
import sqlite3
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, shoot_date TEXT,
 first_capture TEXT, last_capture TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS files (
 session_id TEXT NOT NULL, path TEXT NOT NULL, capture_at TEXT,
 bytes INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'unknown',
 PRIMARY KEY(session_id, path)
);
CREATE TABLE IF NOT EXISTS students (
 session_id TEXT NOT NULL, student_id TEXT NOT NULL, name TEXT NOT NULL,
 PRIMARY KEY(session_id, student_id)
);
CREATE TABLE IF NOT EXISTS selection_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
 file_path TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
 at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS workflow_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
 stage TEXT NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, name TEXT, studio TEXT);
"""
NAV = ['Обзор','Съёмки','Производительность','Качество','Отбор ИИ','Рабочий процесс','История','Профиль','Настройки']
HTML = r"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FoboX 4.0 alpha</title>
<style>
:root{font-family:Inter,system-ui,Arial,sans-serif;color:#f3f4f9;background:#0d1119}
*{box-sizing:border-box}body{margin:0}aside{width:62px;background:#111722;position:fixed;inset:0 auto 0 0;border-right:1px solid #28303b;display:flex;flex-direction:column;transition:width .18s;z-index:3;overflow:hidden}aside.open{width:225px}
.brand{font-size:22px;font-weight:800;padding:21px 12px;white-space:nowrap}.brand b{color:#7738ff}
nav{display:flex;flex-direction:column;gap:4px;padding:6px}
nav button,.profile{background:none;border:0;color:#aeb7c9;display:flex;align-items:center;text-align:left;gap:15px;padding:12px;border-radius:8px;cursor:pointer;white-space:nowrap;width:100%}
nav button.active{background:#292044;color:white;border-left:2px solid #7738ff}nav button:hover,.profile:hover{background:#252d3b}
.ico{font-size:18px;display:inline-block;width:20px;flex-shrink:0;text-align:center}
.profile{margin-top:auto;margin-bottom:10px}.avatar{width:29px;height:29px;border-radius:50%;background:#454d60;display:grid;place-items:center}
main{margin-left:62px;padding:28px 34px;max-width:1600px}header{display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap;margin-bottom:24px}
h1{font-size:27px;margin:0}h2{font-size:16px;margin:0 0 16px}.muted{color:#909bad;font-size:12px}
select{background:#1c2532;color:white;border:1px solid #343e50;border-radius:8px;padding:9px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
.card,.panel{background:#171e2a;border:1px solid #252f3d;border-radius:11px;padding:20px}.label{font-size:12px;color:#a6afbf}.value{font-size:32px;font-weight:700;margin:12px 0}.panel{margin-top:18px}.empty{color:#8491a7;padding:40px 10px;text-align:center;border:1px dashed #3a4556;border-radius:8px}
.tabs{display:flex;gap:8px;flex-wrap:wrap}.tabs button{background:#222b39;border:0;color:#b8c2d3;border-radius:6px;padding:10px 14px;cursor:pointer}.tabs button.active{background:#7738ff;color:white}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:12px;text-align:left;border-bottom:1px solid #2a3443}th{color:#9faabc}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:20px}}@media(max-width:520px){.grid{grid-template-columns:1fr}}
</style>
<aside id="sidebar"><div class="brand" onclick="toggle()" style="cursor:pointer">F<b>X</b><span class="txt"> FoboX 4.0</span></div><nav id="nav"></nav>
<button class="profile" onclick="show('Профиль')"><span class="avatar">◉</span><span class="txt">Профиль фотографа</span></button></aside>
<main><header><div><h1 id="heading">Обзор</h1><div class="muted">FoboX 4.0 · локальная аналитика</div></div>
<div style="display:flex;gap:8px"><select id="period"><option>Месяц</option><option>Год</option><option>Всё время</option></select><select id="compare"><option>К прошлому месяцу</option><option>К прошлому году</option></select></div></header><div id="body"></div></main>
<script>
const pages=['Обзор','Съёмки','Производительность','Качество','Отбор ИИ','Рабочий процесс','История','Профиль','Настройки'];
const icons=['⌂','▱','▥','◇','✓','◷','▤','○','⚙'];
let state={sessions:0,files:0,students:0,selected:0},active='Обзор';
const nav=document.getElementById('nav');
pages.forEach((p,i)=>{let b=document.createElement('button');b.innerHTML='<span class="ico">'+icons[i]+'</span><span class="txt">'+p+'</span>';b.onclick=()=>show(p);b.dataset.page=p;nav.appendChild(b)});
function toggle(){document.getElementById('sidebar').classList.toggle('open')}
function metric(label,value){return '<div class="card"><div class="label">'+label+'</div><div class="value">'+value+'</div><div class="muted">Без сравнительных данных</div></div>'}
function panel(title,description='Данные появятся после регистрации и обработки съёмок.'){return '<section class="panel"><h2>'+title+'</h2><div class="empty">'+description+'</div></section>'}
function show(p){active=p;document.getElementById('heading').textContent=p;nav.querySelectorAll('button').forEach(b=>b.classList.toggle('active',b.dataset.page===p));let out='';
if(p==='Обзор'){out='<div class="grid">'+metric('Проведено съёмок',state.sessions)+metric('Отснято кадров',state.files)+metric('Часы съёмок','—')+metric('Готовые комплекты','—')+'</div>'+panel('Динамика съёмок')+panel('Календарь активности')+panel('Рекорды фотографа')}
if(p==='Съёмки')out=panel('Текущая съёмка','Интерфейс отбора v3 пока работает отдельно. Подключение к v4 запланировано.');
if(p==='Производительность')out='<div class="grid">'+['Кадров в час','Съёмок в день','Учеников за съёмку','Средняя длительность'].map(x=>metric(x,'—')).join('')+'</div>'+panel('Динамика производительности');
if(p==='Качество')out='<div class="grid">'+['Резкие кадры','Дубли','Пересветы','Технический брак'].map(x=>metric(x,'—')).join('')+'</div>'+panel('Динамика качества');
if(p==='Отбор ИИ')out='<div class="grid">'+['Предложено ИИ','Принято без изменений','Заменено вручную','Доля подтверждений'].map(x=>metric(x,'—')).join('')+'</div>'+panel('Точность предложений ИИ');
if(p==='Рабочий процесс')out='<div class="grid">'+['Импорт','Анализ','Проверка ученика','До подтверждения'].map(x=>metric(x,'—')).join('')+'</div>'+panel('Этапы обработки');
if(p==='История')out=panel('История съёмок','Съёмок в локальной базе: '+state.sessions+'.');
if(p==='Профиль')out=panel('Профиль фотографа','Аватар, имя, студия и контакты будут доступны в следующей итерации.');
if(p==='Настройки')out='<div class="tabs">'+['Профиль','Отбор и качество','Файлы и экспорт','Интерфейс','Данные и резервные копии'].map(x=>'<button onclick="document.getElementById(\'settingsDetail\').textContent=\''+x+': параметры будут доступны в следующей итерации.\'">'+x+'</button>').join('')+'</div>'+panel('Настройки')+'<p class="muted" id="settingsDetail"></p>';
document.getElementById('body').innerHTML=out;
}
fetch('/api/summary').then(r=>r.json()).then(x=>{state=x;show(active)}).catch(()=>show(active));show(active);
</script></html>"""

def init(db):
    with sqlite3.connect(db) as conn:
        conn.executescript(SCHEMA)

def summary(db):
    with sqlite3.connect(db) as conn:
        return dict(sessions=conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],
                    files=conn.execute('SELECT COUNT(*) FROM files').fetchone()[0],
                    students=conn.execute('SELECT COUNT(*) FROM students').fetchone()[0],
                    selected=conn.execute("SELECT COUNT(*) FROM selection_events WHERE action='select'").fetchone()[0])

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--port',type=int,default=8766)
    ap.add_argument('--db',default=str(Path.home()/'.fobox'/'fobox4.sqlite3'))
    args=ap.parse_args()
    db=Path(args.db).expanduser()
    db.parent.mkdir(parents=True,exist_ok=True)
    init(db)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path=urlparse(self.path).path
            if path=='/api/summary':
                data=json.dumps(summary(db),ensure_ascii=False).encode()
                mime='application/json'
            elif path=='/':
                data=HTML.encode()
                mime='text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    print(f'FOBOX 4.0 alpha: http://127.0.0.1:{args.port}')
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()

if __name__=='__main__':
    main()
