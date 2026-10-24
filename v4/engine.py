"""FOBOX 4.0 alpha.2 engine: catalog, analysis, selection, export, statistics.

Source files are opened read-only. Nothing in the source folder is ever
renamed, moved, deleted or overwritten; export only creates copies.
Pillow is optional: without it quality analysis reports "no data".
"""
import csv
import hashlib
import io
import json
import mmap
import re
import shutil
import sqlite3
import struct
import threading
from contextlib import contextmanager
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageFilter, ImageStat
    Image.MAX_IMAGE_PIXELS = 200_000_000
except ImportError:  # analysis degrades gracefully
    Image = None

RAW_EXT = {'.arw', '.cr2', '.cr3', '.nef', '.nrw', '.raf', '.rw2', '.orf', '.dng', '.pef',
           '.3fr', '.srw', '.iiq', '.rwl', '.erf', '.k25', '.kdc', '.mos', '.mrw', '.x3f'}
IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'}
PREVIEWABLE = {'.jpg', '.jpeg', '.png', '.webp'}
CATS = ['01_ДОКУМЕНТЫ', '02_ЗДАНИЕ', '03_ОБЩАЯ', '04_РЕПОРТАЖ', '05_УЧЕНИКИ', '06_УЧИТЕЛЯ', '07_ФОН']
FLAG_NAMES = {'white': 'Пустой белый', 'black': 'Пустой чёрный', 'over': 'Пересвет',
              'under': 'Недодержка', 'blur': 'Нерезкий', 'dup': 'Дубль'}

DEFAULT_SETTINGS = {
    'series_gap_sec': '90',        # new series when pause between frames is longer
    'radius': '12',                # school mode: frames around worksheet anchors
    'target_per_student': '20',    # school mode: soft target incl. 5 mandatory
    'blur_threshold': '20',        # strongest-edge level below -> "blur"
    'dup_distance': '5',           # dHash Hamming distance for near-duplicates
    'clip_threshold': '0.25',      # share of clipped pixels for over/under
    'accent': '#7738FF',
    'theme': 'dark',
    'workspace': 'Моя студия',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, shoot_date TEXT,
 first_capture TEXT, last_capture TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS files (
 session_id TEXT NOT NULL, path TEXT NOT NULL, capture_at TEXT,
 bytes INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'unknown',
 PRIMARY KEY(session_id, path));
CREATE TABLE IF NOT EXISTS students (
 session_id TEXT NOT NULL, student_id TEXT NOT NULL, name TEXT NOT NULL,
 PRIMARY KEY(session_id, student_id));
CREATE TABLE IF NOT EXISTS selection_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
 file_path TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
 at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS workflow_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
 stage TEXT NOT NULL, event TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, name TEXT, studio TEXT);
CREATE TABLE IF NOT EXISTS ai_suggestions (
 session_id TEXT NOT NULL, section_id TEXT NOT NULL, file_path TEXT NOT NULL,
 reason TEXT, PRIMARY KEY(session_id, section_id, file_path));
CREATE TABLE IF NOT EXISTS selections (
 session_id TEXT NOT NULL, section_id TEXT NOT NULL, file_path TEXT NOT NULL,
 PRIMARY KEY(session_id, section_id, file_path));
CREATE TABLE IF NOT EXISTS anchor_overrides (
 session_id TEXT NOT NULL, person TEXT NOT NULL, key TEXT NOT NULL, file_path TEXT NOT NULL,
 at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(session_id, person, key));
CREATE TABLE IF NOT EXISTS approvals (
 session_id TEXT NOT NULL, section_id TEXT NOT NULL, approved INTEGER NOT NULL,
 at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(session_id, section_id));
"""
# columns added on top of the alpha.1 schema (migration keeps old databases working)
MIGRATIONS = {
    'sessions': {'source': 'TEXT', 'mode': "TEXT DEFAULT 'general'", 'order_no': 'TEXT',
                 'school': 'TEXT', 'class_name': 'TEXT', 'location': 'TEXT',
                 'status': "TEXT DEFAULT 'imported'", 'meta': 'TEXT'},
    'files': {'name': 'TEXT', 'frame': 'INTEGER', 'preview': 'TEXT', 'camera': 'TEXT',
              'series': 'INTEGER', 'brightness': 'REAL', 'sharpness': 'REAL',
              'clip_hi': 'REAL', 'clip_lo': 'REAL', 'dhash': 'TEXT', 'flags': 'TEXT',
              'analyzed': 'INTEGER DEFAULT 0', 'orient': 'INTEGER'},
    'students': {'role': "TEXT DEFAULT 'student'", 'anchors': 'TEXT'},
    'selection_events': {'section_id': 'TEXT'},
    'profiles': {'email': 'TEXT', 'phone': 'TEXT', 'city': 'TEXT', 'avatar': 'TEXT'},
}
SCHEMA_VERSION = 2


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def frame_number(stem):
    m = re.search(r'(\d+)$', stem)
    return int(m.group(1)) if m else None


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(s)).strip(' .')
    if not s:
        raise ValueError('Пустое имя')
    return s


# ---------------------------------------------------------------- metadata
def _tiff_ifds(buf, base=0):
    """Yield (tags) dicts for every IFD in a TIFF structure (IFD0 chain, SubIFDs, EXIF)."""
    if buf[base:base + 2] == b'II':
        e = '<'
    elif buf[base:base + 2] == b'MM':
        e = '>'
    else:
        return
    first = struct.unpack_from(e + 'I', buf, base + 4)[0]
    seen, todo = set(), [first]
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
    while todo and len(seen) < 64:
        off = todo.pop()
        if not off or off in seen or base + off + 2 > len(buf):
            continue
        seen.add(off)
        p = base + off
        n = struct.unpack_from(e + 'H', buf, p)[0]
        if n > 1000:
            continue
        tags = {}
        for i in range(n):
            q = p + 2 + i * 12
            if q + 12 > len(buf):
                break
            tag, typ, cnt = struct.unpack_from(e + 'HHI', buf, q)
            size = sizes.get(typ, 1) * cnt
            vpos = q + 8 if size <= 4 else base + struct.unpack_from(e + 'I', buf, q + 8)[0]
            if typ == 2:
                tags[tag] = bytes(buf[vpos:vpos + cnt]).split(b'\0')[0].decode('latin-1', 'replace').strip()
            elif typ in (3, 4, 13) and cnt >= 1 and vpos + sizes.get(typ, 4) * cnt <= len(buf):
                fmt = 'H' if typ == 3 else 'I'
                vals = struct.unpack_from(e + fmt * min(cnt, 64), buf, vpos)
                tags[tag] = vals if len(vals) > 1 else vals[0]
        yield tags
        nxt_pos = p + 2 + n * 12
        if nxt_pos + 4 <= len(buf):
            todo.append(struct.unpack_from(e + 'I', buf, nxt_pos)[0])
        for sub in (0x8769, 0x014A):
            v = tags.get(sub)
            if v:
                todo.extend(v if isinstance(v, tuple) else [v])


def read_meta(path):
    """Return dict(capture_at, camera, jpeg=(offset,length)) without external libraries."""
    out = {'capture_at': None, 'camera': None, 'jpeg': None, 'orient': None}
    try:
        with open(path, 'rb') as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                base = 0
                if mm[:2] == b'\xff\xd8':  # JPEG: locate EXIF APP1
                    i = mm.find(b'Exif\0\0', 0, 65536)
                    if i < 0:
                        return out
                    base = i + 6
                elif mm[:4] == b'FUJI':  # RAF: embedded JPEG offset in header
                    off, ln = struct.unpack_from('>II', mm, 84)
                    out['jpeg'] = (off, ln)
                    j = mm.find(b'Exif\0\0', off, off + 65536)
                    if j < 0:
                        return out
                    base = j + 6
                best = None
                for tags in _tiff_ifds(mm, base):
                    dt = tags.get(0x9003) or tags.get(0x0132)
                    if dt and (out['capture_at'] is None or tags.get(0x9003)):
                        out['capture_at'] = dt
                    if isinstance(tags.get(0x0112), int) and out['orient'] is None and 1 <= tags[0x0112] <= 8:
                        out['orient'] = tags[0x0112]  # first (main) IFD: how the camera was held
                    if tags.get(0x0110) and not out['camera']:
                        out['camera'] = ' '.join(filter(None, [tags.get(0x010F, ''), tags[0x0110]])).strip()
                    o, ln = tags.get(0x0201), tags.get(0x0202)
                    if isinstance(o, int) and isinstance(ln, int) and ln > 1000 and base + o + ln <= len(mm):
                        if not best or ln > best[1]:
                            best = (base + o, ln)
                    # DNG/ARW previews stored as strips with JPEG compression (6/7)
                    if tags.get(0x0103) in (6, 7) and isinstance(tags.get(0x0111), int) and isinstance(tags.get(0x0117), int):
                        o, ln = tags[0x0111], tags[0x0117]
                        if ln > 1000 and base + o + ln <= len(mm) and mm[base + o:base + o + 2] == b'\xff\xd8':
                            if not best or ln > best[1]:
                                best = (base + o, ln)
                if best and not out['jpeg']:
                    out['jpeg'] = best
            finally:
                mm.close()
    except (OSError, ValueError, struct.error):
        pass
    if out['capture_at']:
        try:
            out['capture_at'] = datetime.strptime(out['capture_at'][:19], '%Y:%m:%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            out['capture_at'] = None
    return out


# ---------------------------------------------------------------- analysis
def analyze_image(img):
    g = img.convert('L')
    g.thumbnail((256, 256))
    st = ImageStat.Stat(g)
    mean, std = st.mean[0], st.stddev[0]
    hist = g.histogram()
    total = sum(hist) or 1
    clip_hi = sum(hist[250:]) / total
    clip_lo = sum(hist[:6]) / total
    # sharpness = strength of the strongest 1% edges; robust to plain studio backdrops
    edges = g.filter(ImageFilter.FIND_EDGES).crop((1, 1, g.width - 1, g.height - 1))
    eh, acc, sharp = edges.histogram(), 0, 0
    need = sum(eh) * 0.01
    for i in range(255, -1, -1):
        acc += eh[i]
        if acc >= need:
            sharp = i
            break
    d = g.resize((9, 8)).getdata()
    px = list(d)
    bits = ''.join('1' if px[r * 9 + c] > px[r * 9 + c + 1] else '0' for r in range(8) for c in range(8))
    return {'brightness': round(mean, 2), 'std': std, 'sharpness': round(sharp, 2),
            'clip_hi': round(clip_hi, 4), 'clip_lo': round(clip_lo, 4), 'dhash': '%016x' % int(bits, 2)}


def hamming(a, b):
    if not a or not b:
        return 99
    return bin(int(a, 16) ^ int(b, 16)).count('1')


# ---------------------------------------------------------------- worksheets (Excel / CSV)
def _norm(h):
    h = re.sub(r'\(.*?\)', '', str(h or ''))  # 'Портрет 1 (ЛР)' -> 'Портрет 1'
    return re.sub(r'[\s._\-№#:]+', '', h.strip().casefold()).replace('ё', 'е')


# accepted column titles -> canonical key (compared without spaces, case, dots)
STUDENT_COLS = {
    'Name': ['name', 'фио', 'фамилияимя', 'фамилияимяотчество', 'ученик', 'имя', 'ребенок', 'фамилия'],
    'V': ['v', 'в', 'виньетка', 'вин', 'vignette'],
    'P1': ['p1', 'п1', 'портрет1', 'лр', 'личныйразворот', 'портретлр'],
    'P2': ['p2', 'п2', 'портрет2', 'лс', 'личнаястраница', 'портретлс'],
    'D1': ['d1', 'д1', 'друг1', 'друзья1', 'сдругом1'],
    'D2': ['d2', 'д2', 'друг2', 'друзья2', 'сдругом2'],
}
# optional columns of the combined worksheet (students and teachers in one file)
EXTRA_COLS = {
    'Type': ['type', 'тип', 'категория', 'кто'],
    'T': ['фотоучителя', 'учитель', 'фотоучитель', 'teacher', 'кадручителя'],
    'Role': ['role', 'должность', 'роль', 'предмет'],
}
ROLE_NAMES = {'V': 'Виньетка', 'P1': 'Личный разворот', 'P2': 'Личная страница', 'D1': 'Друг 1', 'D2': 'Друг 2', 'T': 'Фото учителя'}
ANCHOR_LABELS = {'V': 'Виньетка', 'P1': 'ЛР', 'P2': 'ЛС', 'D1': 'Друг 1', 'D2': 'Друг 2', 'T': 'Фото учителя'}
TEACHER_COLS = {
    'Name': ['name', 'фио', 'учитель', 'педагог', 'имя'],
    'Role': ['role', 'должность', 'роль', 'предмет'],
    'Start': ['start', 'начало', 'с', 'первыйкадр', 'от'],
    'End': ['end', 'конец', 'по', 'последнийкадр', 'до'],
}


def _cell(v):
    """Excel stores 1750 as '1750.0' or '1750' -> '1750'; keeps file names like ANT01750 as is."""
    v = str(v if v is not None else '').strip()
    if re.fullmatch(r'\d+\.0+', v):
        v = v.split('.')[0]
    return v


def _xlsx_rows(path):
    import zipfile
    import xml.etree.ElementTree as ET
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    rel_ns = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', ns):
                shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % ns['m'])))
        # first sheet in workbook order
        sheet = 'xl/worksheets/sheet1.xml'
        try:
            wb = ET.fromstring(z.read('xl/workbook.xml'))
            first = wb.find('m:sheets/m:sheet', ns)
            rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
            for r in rels:
                if r.get('Id') == first.get(rel_ns):
                    t = r.get('Target').lstrip('/')
                    sheet = t if t.startswith('xl/') else 'xl/' + t
        except (KeyError, AttributeError, ET.ParseError):
            pass
        rows = []
        for row in ET.fromstring(z.read(sheet)).iter('{%s}row' % ns['m']):
            vals = {}
            for cell in row.findall('m:c', ns):
                ref = re.match(r'([A-Z]+)', cell.get('r', 'A'))[1]
                col = 0
                for ch in ref:
                    col = col * 26 + ord(ch) - 64
                typ, v = cell.get('t'), cell.find('m:v', ns)
                if typ == 's' and v is not None:
                    val = shared[int(v.text)]
                elif typ == 'inlineStr':
                    val = ''.join(t.text or '' for t in cell.iter('{%s}t' % ns['m']))
                else:
                    val = v.text if v is not None else ''
                vals[col - 1] = val
            if vals:
                rows.append([vals.get(i, '') for i in range(max(vals) + 1)])
        return rows


def _csv_rows(path):
    raw = Path(path).read_bytes()
    for enc in ('utf-8-sig', 'cp1251'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    first = text.splitlines()[0] if text else ''
    delim = max([';', ',', '\t'], key=first.count)  # Russian Excel saves CSV with ';'
    return list(csv.reader(io.StringIO(text), delimiter=delim))


def _looks_like_name(v):
    v = str(v or '').strip()
    return bool(re.search(r'[A-Za-zА-Яа-яЁё]{2,}', v)) and not re.fullmatch(r'[A-Za-z]{2,5}\d+(\.\w+)?', v)


def read_worksheet(path, columns, optional=None):
    """Read an Excel (.xlsx) or CSV worksheet.
    * The header row is searched in the first 15 rows; Russian/English titles are accepted.
    * A CSV opened in Excel without splitting (all text in column A with ';') is split.
    * Without a recognisable header the layout is taken by position:
      [type;] name; V; P1; P2; D1; D2 [; teacher photo]."""
    p = Path(str(path).strip().strip('"')).expanduser()
    if not p.is_file():
        raise ValueError('Не найдена ведомость: ' + str(p))
    ext = p.suffix.lower()
    if ext in ('.xlsx', '.xlsm'):
        rows = _xlsx_rows(p)
    elif ext == '.xls':
        raise ValueError('Старый формат .xls: откройте файл в Excel и сохраните как .xlsx')
    else:
        rows = _csv_rows(p)
    # CSV pasted into one Excel column: "ученики;Акимова Мария;1750;..."
    filled = [r for r in rows if any(str(x).strip() for x in r)]
    if filled and sum(1 for r in filled if len([x for x in r if str(x).strip()]) == 1 and ';' in str(next(x for x in r if str(x).strip()))) > len(filled) * 0.6:
        rows = [str(next((x for x in r if str(x).strip()), '')).split(';') for r in rows]
    optional = optional or {}
    allcols = {**columns, **optional}
    alias = {_norm(a): key for key, names in allcols.items() for a in names + [key]}
    mapping, hi = None, -1
    for i, row in enumerate(rows[:15]):
        m = {}
        for j, h in enumerate(row):
            key = alias.get(_norm(h))
            if key and key not in m.values():
                m[j] = key
        if set(m.values()) >= set(columns):
            mapping, hi = m, i
            break
    out = []
    if mapping is not None:
        for row in rows[hi + 1:]:
            rec = {key: _cell(row[i]) if i < len(row) else '' for i, key in mapping.items()}
            if rec.get('Name'):
                out.append(rec)
        return out
    if 'V' not in columns:
        expected = ', '.join(names[1] if len(names) > 1 else names[0] for names in columns.values())
        raise ValueError(f'{p.name}: не найдена строка заголовков. Нужны колонки: {expected}')
    # no header: name in the first text cell, then five frame columns in order
    order = ['V', 'P1', 'P2', 'D1', 'D2', 'T']
    for row in rows:
        cells = [_cell(x) for x in row]
        idx = next((i for i, v in enumerate(cells) if _looks_like_name(v) and not re.match(r'(?i)(ученик|учител|тип|фамил|фио|name)', v)), None)
        if idx is None:
            continue
        rec = {'Name': cells[idx], 'Type': next((v for v in cells[:idx] if v), '')}
        for k, v in zip(order, cells[idx + 1:]):
            rec[k] = v
        if any(rec.get(k) for k in order):
            out.append(rec)
    if not out:
        raise ValueError(f'{p.name}: не удалось прочитать. Нужно: Фамилия Имя и 5 номеров (Виньетка, ЛР, ЛС, Друг 1, Друг 2)')
    return out


class Engine:
    def __init__(self, db_path, cache_dir=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = Path(cache_dir) if cache_dir else self.db_path.parent / 'previews'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        self._migrate()

    # -------------------------------------------------------- db helpers
    @contextmanager
    def conn(self):
        """Transaction that commits on success, rolls back on error and always closes."""
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA journal_mode=WAL')
            with c:
                yield c
        finally:
            c.close()

    def _migrate(self):
        with self.conn() as c:
            c.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
                for col, decl in cols.items():
                    if col not in have:
                        c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')
            for k, v in DEFAULT_SETTINGS.items():
                c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            c.execute("INSERT OR IGNORE INTO profiles(id,name,studio) VALUES('me','','')")

    def settings(self):
        with self.conn() as c:
            return {r['key']: r['value'] for r in c.execute('SELECT key,value FROM settings')}

    def setting(self, key, cast=str):
        return cast(self.settings().get(key, DEFAULT_SETTINGS.get(key)))

    def save_settings(self, data):
        allowed = set(DEFAULT_SETTINGS)
        with self.conn() as c:
            for k, v in data.items():
                if k not in allowed:
                    raise ValueError('Неизвестная настройка: ' + k)
                if k == 'accent' and not re.fullmatch(r'#[0-9A-Fa-f]{6}', str(v)):
                    raise ValueError('Цвет в формате #RRGGBB')
                if k in ('series_gap_sec', 'radius', 'target_per_student', 'dup_distance'):
                    if not str(v).isdigit() or int(v) <= 0:
                        raise ValueError('Положительное целое число: ' + k)
                c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (k, str(v)))

    def profile(self):
        with self.conn() as c:
            return dict(c.execute("SELECT * FROM profiles WHERE id='me'").fetchone())

    def save_profile(self, data):
        fields = ['name', 'studio', 'email', 'phone', 'city']
        with self.conn() as c:
            c.execute('UPDATE profiles SET ' + ','.join(f'{f}=?' for f in fields) + " WHERE id='me'",
                      [str(data.get(f, ''))[:200] for f in fields])

    def event(self, c, sid, stage, ev):
        c.execute('INSERT INTO workflow_events(session_id,stage,event,at) VALUES(?,?,?,?)', (sid, stage, ev, now()))

    # -------------------------------------------------------- import
    @staticmethod
    def session_id(source):
        norm = str(Path(source).resolve()).casefold()
        return hashlib.sha1(norm.encode('utf-8')).hexdigest()[:12]

    def start_import(self, opts):
        src = Path(str(opts.get('source', '')).strip().strip('"')).expanduser()
        if not src.is_dir():
            raise ValueError('Папка с исходниками не найдена: ' + str(src))
        sid = self.session_id(src)
        job = {'session': sid, 'stage': 'Сканирование', 'done': 0, 'total': 0, 'error': None, 'finished': False}
        self.jobs[sid] = job
        threading.Thread(target=self._import_job, args=(src, opts, job), daemon=True).start()
        return {'session': sid}

    def _import_job(self, src, opts, job):
        try:
            self.import_folder(src, opts, job)
        except Exception as e:  # report to UI instead of dying silently
            job['error'] = str(e)
        finally:
            job['finished'] = True

    def import_folder(self, src, opts=None, job=None):
        opts = opts or {}
        job = job if job is not None else {}
        src = Path(src).resolve()
        sid = self.session_id(src)
        raws, imgs = [], defaultdict(list)
        for p in src.rglob('*'):
            if not p.is_file() or p.name.startswith('._'):
                continue
            ext = p.suffix.lower()
            if ext in RAW_EXT:
                raws.append(p)
            elif ext in IMG_EXT:
                imgs[(str(p.parent), p.stem.casefold())].append(p)
        # a "photo" is a RAW (with optional JPEG twin) or a standalone JPEG/PNG.
        # The twin is looked up in the same folder first, then anywhere on the card by name
        # (cameras/copy tools often keep RAW and JPEG in separate folders).
        by_stem = defaultdict(list)
        for (_, stem), group in imgs.items():
            by_stem[stem].extend(x for x in group if x.suffix.lower() in PREVIEWABLE)
        raw_stems = Counter(r.stem.casefold() for r in raws)
        items = []
        used = set()
        for r in raws:
            st = r.stem.casefold()
            side = next((x for x in imgs.get((str(r.parent), st), []) if x.suffix.lower() in PREVIEWABLE), None)
            if side is None and raw_stems[st] == 1 and len(by_stem[st]) == 1:
                side = by_stem[st][0]
            if side:
                used.add(side)
            items.append((r, 'raw', side))
        for group in imgs.values():
            for p in group:
                if p not in used:
                    items.append((p, 'image', p if p.suffix.lower() in PREVIEWABLE else None))
        if not items:
            raise ValueError('В папке нет RAW или JPEG')
        job.update(stage='Чтение EXIF', total=len(items), done=0)
        mode = 'school' if opts.get('students') else 'general'
        title = (opts.get('title') or '').strip() or src.name
        with self.conn() as c:
            self.event(c, sid, 'import', 'start')
            c.execute('''INSERT INTO sessions(id,title,source,mode,order_no,school,class_name,location,status)
                         VALUES(?,?,?,?,?,?,?,?,'imported')
                         ON CONFLICT(id) DO UPDATE SET title=excluded.title, mode=excluded.mode,
                         order_no=excluded.order_no, school=excluded.school, class_name=excluded.class_name,
                         location=excluded.location''',
                      (sid, title, str(src), mode, opts.get('order', ''), opts.get('school', ''),
                       opts.get('class_name', ''), opts.get('location', '')))
            present = set()
            for i, (p, kind, side) in enumerate(items):
                meta = read_meta(side or p) if side else read_meta(p)
                if side and not meta['capture_at']:
                    meta.update({k: v for k, v in read_meta(p).items() if v and k != 'jpeg'})
                if not side and kind == 'raw':
                    rm = read_meta(p)
                    meta = {**meta, **{k: v for k, v in rm.items() if v}}
                cap = meta['capture_at'] or datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                preview = str(side) if side else (f"embedded:{meta['jpeg'][0]}:{meta['jpeg'][1]}" if meta.get('jpeg') else None)
                key = str(p)
                present.add(key)
                orient = meta.get('orient') if not side else None
                if kind == 'raw' and not side:
                    orient = read_meta(p).get('orient')
                c.execute('''INSERT INTO files(session_id,path,capture_at,bytes,kind,name,frame,preview,camera,orient)
                             VALUES(?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(session_id,path) DO UPDATE SET capture_at=excluded.capture_at,
                             bytes=excluded.bytes, preview=excluded.preview, camera=excluded.camera, orient=excluded.orient''',
                          (sid, key, cap, p.stat().st_size, kind, p.name, frame_number(p.stem), preview, meta['camera'], orient))
                job['done'] = i + 1
            # files that disappeared from the source are dropped from the catalog (never from disk)
            for r in c.execute('SELECT path FROM files WHERE session_id=?', (sid,)).fetchall():
                if r['path'] not in present:
                    c.execute('DELETE FROM files WHERE session_id=? AND path=?', (sid, r['path']))
            self._assign_series(c, sid)
            span = c.execute('SELECT MIN(capture_at) a, MAX(capture_at) b FROM files WHERE session_id=?', (sid,)).fetchone()
            c.execute('UPDATE sessions SET first_capture=?, last_capture=?, shoot_date=? WHERE id=?',
                      (span['a'], span['b'], (opts.get('date') or (span['a'] or '')[:10]), sid))
            if mode == 'school':
                self._load_people(c, sid, opts.get('students'), opts.get('teachers'))
            self.event(c, sid, 'import', 'end')
        self.analyze(sid, job)
        self.suggest(sid)
        job['stage'] = 'Готово'
        return sid

    def _assign_series(self, c, sid):
        gap = self.setting('series_gap_sec', int)
        rows = c.execute('SELECT path,capture_at FROM files WHERE session_id=? ORDER BY capture_at,name', (sid,)).fetchall()
        series, prev = 0, None
        for r in rows:
            t = datetime.strptime(r['capture_at'], '%Y-%m-%d %H:%M:%S')
            if prev and (t - prev).total_seconds() > gap:
                series += 1
            prev = t
            c.execute('UPDATE files SET series=? WHERE session_id=? AND path=?', (series + 1, sid, r['path']))

    def _load_people(self, c, sid, students_file, teachers_file):
        c.execute('DELETE FROM students WHERE session_id=?', (sid,))
        ns = nt = 0
        rows = read_worksheet(students_file, STUDENT_COLS, EXTRA_COLS) if students_file else []
        for r in rows:
            typ = (r.get('Type') or '').casefold()
            is_teacher = typ.startswith(('учит', 'педаг', 'teach')) or \
                (r.get('T') and not any(r.get(k) for k in ('V', 'P1', 'P2', 'D1', 'D2')))
            if is_teacher:
                anchors = {'Role': r.get('Role', ''), 'T': r.get('T') or ' '.join(r.get(k, '') for k in ('V', 'P1', 'P2', 'D1', 'D2')).strip()}
                c.execute('INSERT INTO students(session_id,student_id,name,role,anchors) VALUES(?,?,?,?,?)',
                          (sid, f't{nt}', safe_name(' '.join(r['Name'].split())), 'teacher', json.dumps(anchors, ensure_ascii=False)))
                nt += 1
                continue
            if typ and not typ.startswith(('учен', 'дет', 'реб', 'stud', 'kid')):
                continue  # other categories (документы, общая …) are not personal pages
            anchors = {k: r.get(k, '') for k in ('V', 'P1', 'P2', 'D1', 'D2')}
            c.execute('INSERT INTO students(session_id,student_id,name,role,anchors) VALUES(?,?,?,?,?)',
                      (sid, f's{ns}', safe_name(' '.join(r['Name'].split()[:2])), 'student', json.dumps(anchors, ensure_ascii=False)))
            ns += 1
        if students_file and not (ns or nt):
            raise ValueError('В ведомости не найдено ни одного ученика')
        for r in read_worksheet(teachers_file, TEACHER_COLS) if teachers_file else []:
            anchors = {'Role': r.get('Role', ''), 'Start': r.get('Start', ''), 'End': r.get('End', '')}
            c.execute('INSERT INTO students(session_id,student_id,name,role,anchors) VALUES(?,?,?,?,?)',
                      (sid, f't{nt}', safe_name(r['Name']), 'teacher', json.dumps(anchors, ensure_ascii=False)))
            nt += 1

    # -------------------------------------------------------- previews
    def preview_bytes(self, sid, path, size=640):
        with self.conn() as c:
            r = c.execute('SELECT preview, orient FROM files WHERE session_id=? AND path=?', (sid, path)).fetchone()
        if not r or not r['preview']:
            return None
        key = hashlib.sha1(f'{r["preview"]}|{size}|o{r["orient"]}|v2'.encode()).hexdigest()
        cached = self.cache / f'{key}.jpg'
        if cached.exists():
            return cached.read_bytes()
        data = self._raw_preview(path, r['preview'])
        if data is None:
            return None
        if Image is not None:
            try:
                im = Image.open(io.BytesIO(data))
                own = im.getexif().get(0x0112)
                im = self._orient(im)
                # previews embedded in RAW carry no orientation; use the RAW's own tag
                if not own and r['orient'] in (3, 6, 8):
                    im = im.transpose({3: Image.Transpose.ROTATE_180, 6: Image.Transpose.ROTATE_270,
                                       8: Image.Transpose.ROTATE_90}[r['orient']])
                im.thumbnail((size, size))
                buf = io.BytesIO()
                im.convert('RGB').save(buf, 'JPEG', quality=82)
                data = buf.getvalue()
            except Exception:
                pass
        tmp = cached.with_suffix('.tmp')
        tmp.write_bytes(data)
        tmp.replace(cached)
        return data

    @staticmethod
    def _orient(im):
        try:
            from PIL import ImageOps
            return ImageOps.exif_transpose(im)
        except Exception:
            return im

    @staticmethod
    def _raw_preview(path, preview):
        try:
            if preview.startswith('embedded:'):
                _, off, ln = preview.split(':')
                with open(path, 'rb') as f:
                    f.seek(int(off))
                    return f.read(int(ln))
            return Path(preview).read_bytes()
        except (OSError, ValueError):
            return None

    # -------------------------------------------------------- quality
    def analyze(self, sid, job=None):
        job = job if job is not None else {}
        with self.conn() as c:
            rows = c.execute('SELECT path,preview,series FROM files WHERE session_id=? ORDER BY capture_at,name', (sid,)).fetchall()
        if Image is None:
            job['stage'] = 'Анализ недоступен (нет Pillow)'
            return
        job.update(stage='Проверка качества', total=len(rows), done=0)
        st = self.settings()
        blur_t, clip_t, dup_d = float(st['blur_threshold']), float(st['clip_threshold']), int(st['dup_distance'])
        results = []
        with self.conn() as c:
            self.event(c, sid, 'analysis', 'start')
        for i, r in enumerate(rows):
            res = None
            data = self.preview_bytes(sid, r['path'], 320)
            if data:
                try:
                    res = analyze_image(Image.open(io.BytesIO(data)))
                except Exception:
                    res = None
            results.append((r, res))
            job['done'] = i + 1
        # sharpness is judged relative to the series median, so a soft-light series is not all "blur"
        by_series = defaultdict(list)
        for r, res in results:
            if res:
                by_series[r['series']].append(res['sharpness'])
        med = {s: sorted(v)[len(v) // 2] for s, v in by_series.items()}
        prev = None
        with self.conn() as c:
            for r, res in results:
                if not res:
                    c.execute('UPDATE files SET analyzed=0, flags=NULL WHERE session_id=? AND path=?', (sid, r['path']))
                    continue
                flags = []
                if res['brightness'] > 242 and res['std'] < 12:
                    flags.append('white')
                elif res['brightness'] < 12 and res['std'] < 12:
                    flags.append('black')
                else:
                    if res['clip_hi'] > clip_t:
                        flags.append('over')
                    if res['clip_lo'] > clip_t * 2 or res['brightness'] < 35:
                        flags.append('under')
                    m = med.get(r['series']) or 0
                    if res['sharpness'] < blur_t or (m and res['sharpness'] < m * 0.45):
                        flags.append('blur')
                    if not flags and prev and prev[0]['series'] == r['series'] and hamming(prev[1]['dhash'], res['dhash']) <= dup_d:
                        # near-identical pair (bracketing, burst): keep the sharper one
                        if res['sharpness'] <= prev[1]['sharpness']:
                            flags.append('dup')
                        else:
                            c.execute("UPDATE files SET flags='dup' WHERE session_id=? AND path=?", (sid, prev[0]['path']))
                            prev = (r, res)
                    elif not flags:
                        prev = (r, res)
                c.execute('''UPDATE files SET brightness=?, sharpness=?, clip_hi=?, clip_lo=?, dhash=?,
                             flags=?, analyzed=1 WHERE session_id=? AND path=?''',
                          (res['brightness'], res['sharpness'], res['clip_hi'], res['clip_lo'], res['dhash'],
                           ','.join(flags) or None, sid, r['path']))
            c.execute("UPDATE sessions SET status=CASE WHEN status='imported' THEN 'analyzed' ELSE status END WHERE id=?", (sid,))
            self.event(c, sid, 'analysis', 'end')

    # -------------------------------------------------------- sections
    def _files(self, c, sid):
        return [dict(r) for r in c.execute('SELECT * FROM files WHERE session_id=? ORDER BY capture_at,name', (sid,))]

    @staticmethod
    def _tokens(val):
        """'1100 1107', '1100, 1107', '1100/1107' -> ['1100', '1107']"""
        return [t for t in re.split(r'[\s,;/+]+', str(val or '').strip()) if t]

    def _resolve(self, files, val, conflicts, ctx=''):
        val = str(val or '').strip()
        if not val:
            return None
        n = frame_number(Path(val).stem)
        cands = [f for f in files if f['kind'] == 'raw' and n is not None and f['frame'] == n] or \
                [f for f in files if n is not None and f['frame'] == n]
        if len(cands) == 1:
            return cands[0]
        exact = [f for f in cands if f['name'].casefold() in (val.casefold(),) or Path(f['name']).stem.casefold() == Path(val).stem.casefold()]
        if len(exact) == 1:
            return exact[0]
        conflicts.append(f'{ctx}{val} — ' + ('нет в папке' if not cands else f'несколько файлов ({len(cands)})'))
        return None

    def _anchors(self, files, a, cols, conflicts, who, overrides=None):
        """Mandatory frames of a section: worksheet numbers, replaced by roles the operator
        reassigned in the app (overrides: key -> path, '' = removed as a wrong frame)."""
        must = {}
        overrides = overrides or {}
        for k in cols:
            if k in overrides:
                if overrides[k] and any(f['path'] == overrides[k] for f in files):
                    must.setdefault(overrides[k], k)
                continue
            for t in self._tokens(a.get(k)):
                f = self._resolve(files, t, conflicts, f'{who}, {ANCHOR_LABELS.get(k, k)}: ')
                if f:
                    must.setdefault(f['path'], k)
        return must

    def sections(self, sid):
        """Review sections. School mode: per student portraits/friends + teachers. General: per series."""
        with self.conn() as c:
            files = self._files(c, sid)
            people = [dict(r) for r in c.execute('SELECT * FROM students WHERE session_id=? ORDER BY rowid', (sid,))]
            ov = defaultdict(dict)
            for r in c.execute('SELECT person,key,file_path FROM anchor_overrides WHERE session_id=?', (sid,)):
                ov[r['person']][r['key']] = r['file_path']
        radius = self.setting('radius', int)
        # when the card has RAW files, standalone JPEGs are not offered for orders (RAW only)
        if any(f['kind'] == 'raw' for f in files):
            files = [f for f in files if f['kind'] == 'raw']
        out, conflicts = [], []
        if people:
            frames = [f for f in files if f['frame'] is not None]
            for p in people:
                a = json.loads(p['anchors'] or '{}')
                if p['role'] == 'student':
                    for suffix, cols, label in (('p', ['V', 'P1', 'P2'], 'Портреты / виньетка'), ('f', ['D1', 'D2'], 'Друзья')):
                        o = {k: v for k, v in ov[p['student_id']].items() if k in cols}
                        must = self._anchors(files, a, cols, conflicts, p['name'], o)
                        # the window stays around the worksheet numbers even if a frame was reassigned
                        sheet = self._anchors(files, a, cols, [], p['name'])
                        nums = [f['frame'] for f in files if (f['path'] in must or f['path'] in sheet) and f['frame'] is not None]
                        near = [f for f in frames if nums and min(abs(f['frame'] - n) for n in nums) <= radius]
                        if not must and not o:
                            continue  # empty cells are fine: no friends photo, child absent, etc.
                        out.append({'id': p['student_id'] + suffix, 'title': p['name'], 'subtitle': label,
                                    'kind': 'portrait' if suffix == 'p' else 'friends', 'person': p['student_id'],
                                    'files': [f['path'] for f in near], 'mandatory': must, 'limit': None,
                                    'roles': cols, 'overridden': bool(o)})
                elif a.get('T'):
                    # teacher given by photo number(s): those frames are mandatory, neighbours shown for choice
                    o = {k: v for k, v in ov[p['student_id']].items() if k == 'T'}
                    must = self._anchors(files, a, ['T'], conflicts, p['name'], o)
                    sheet = self._anchors(files, a, ['T'], [], p['name'])
                    nums = [f['frame'] for f in files if (f['path'] in must or f['path'] in sheet) and f['frame'] is not None]
                    near = [f for f in frames if nums and min(abs(f['frame'] - n) for n in nums) <= max(3, radius // 2)]
                    if not must and not o:
                        continue
                    out.append({'id': p['student_id'], 'title': p['name'], 'subtitle': a.get('Role') or 'Учитель',
                                'kind': 'teacher', 'person': p['student_id'],
                                'files': [f['path'] for f in near], 'mandatory': must, 'limit': 5,
                                'roles': ['T'], 'overridden': bool(o)})
                else:
                    s0 = self._resolve(files, a.get('Start'), conflicts, p['name'] + ', начало: ')
                    s1 = self._resolve(files, a.get('End'), conflicts, p['name'] + ', конец: ')
                    lo, hi = sorted([s0['frame'], s1['frame']]) if s0 and s1 else (0, -1)
                    out.append({'id': p['student_id'], 'title': p['name'], 'subtitle': a.get('Role') or 'Учитель',
                                'kind': 'teacher', 'person': p['student_id'],
                                'files': [f['path'] for f in frames if lo <= f['frame'] <= hi], 'mandatory': {}, 'limit': 5,
                                'roles': [], 'overridden': False})
        else:
            by = defaultdict(list)
            for f in files:
                by[f['series']].append(f)
            for s, fs in sorted(by.items()):
                t0, t1 = fs[0]['capture_at'][11:16], fs[-1]['capture_at'][11:16]
                out.append({'id': f'g{s}', 'title': f'Серия {s}', 'subtitle': f'{t0}–{t1} · {len(fs)} кадр.',
                            'kind': 'series', 'person': None, 'files': [f['path'] for f in fs], 'mandatory': {}, 'limit': None,
                            'roles': [], 'overridden': False})
        return out, conflicts

    # -------------------------------------------------------- automatic suggestions
    def suggest(self, sid):
        """Heuristic pre-selection: never touches mandatory frames, skips flagged frames,
        prefers sharp frames that differ from already chosen ones."""
        secs, _ = self.sections(sid)
        target = self.setting('target_per_student', int)
        dup_d = self.setting('dup_distance', int)
        with self.conn() as c:
            info = {f['path']: f for f in self._files(c, sid)}
            c.execute('DELETE FROM ai_suggestions WHERE session_id=?', (sid,))
            approved = {r['section_id'] for r in c.execute('SELECT section_id FROM approvals WHERE session_id=? AND approved=1', (sid,))}
            has_sel = {r['section_id'] for r in c.execute('SELECT DISTINCT section_id FROM selections WHERE session_id=?', (sid,))}
            for s in secs:
                if s['kind'] == 'portrait':
                    quota = max(0, target - 5) * 2 // 3
                elif s['kind'] == 'friends':
                    quota = max(0, target - 5) - max(0, target - 5) * 2 // 3
                elif s['kind'] == 'teacher':
                    quota = 3
                else:
                    quota = max(1, round(len(s['files']) * 0.35))
                chosen = [info[p] for p in s['mandatory'] if p in info]
                pool = [info[p] for p in s['files'] if p in info and p not in s['mandatory'] and not info[p]['flags']]
                pool.sort(key=lambda f: -(f['sharpness'] or 0))
                picks = []
                for f in pool:
                    if len(picks) >= quota:
                        break
                    if all(hamming(f['dhash'], x['dhash']) > dup_d + 3 for x in chosen + picks):
                        picks.append(f)
                for f in picks:
                    c.execute('INSERT OR IGNORE INTO ai_suggestions VALUES(?,?,?,?)', (sid, s['id'], f['path'], 'diverse+sharp'))
                # pre-fill selection only for sections the person has not touched yet
                if s['id'] not in has_sel and s['id'] not in approved:
                    for p in list(s['mandatory']) + [f['path'] for f in picks]:
                        c.execute('INSERT OR IGNORE INTO selections VALUES(?,?,?)', (sid, s['id'], p))
                        c.execute('INSERT INTO selection_events(session_id,file_path,actor,action,section_id,at) VALUES(?,?,?,?,?,?)',
                                  (sid, p, 'worksheet' if p in s['mandatory'] else 'auto', 'select', s['id'], now()))

    # -------------------------------------------------------- review state
    def session(self, sid):
        with self.conn() as c:
            s = c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if not s:
                raise ValueError('Съёмка не найдена')
            files = self._files(c, sid)
            sel = defaultdict(list)
            for r in c.execute('SELECT section_id,file_path FROM selections WHERE session_id=?', (sid,)):
                sel[r['section_id']].append(r['file_path'])
            appr = {r['section_id']: bool(r['approved']) for r in c.execute('SELECT section_id,approved FROM approvals WHERE session_id=?', (sid,))}
            sugg = defaultdict(list)
            for r in c.execute('SELECT section_id,file_path FROM ai_suggestions WHERE session_id=?', (sid,)):
                sugg[r['section_id']].append(r['file_path'])
        secs, conflicts = self.sections(sid)
        with self.conn() as c:
            people = c.execute("SELECT student_id, name FROM students WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
        present = {x['person'] for x in secs}
        absent = [r['name'] for r in people if r['student_id'] not in present]
        slim = [{k: f[k] for k in ('path', 'name', 'capture_at', 'kind', 'frame', 'series', 'flags', 'sharpness', 'brightness', 'analyzed')}
                | {'has_preview': bool(f['preview'])} for f in files]
        return {'session': dict(s), 'files': slim, 'sections': secs, 'conflicts': conflicts,
                'selected': sel, 'approved': appr, 'absent': absent, 'suggested': sugg, 'pillow': Image is not None,
                'flag_names': FLAG_NAMES}

    def review(self, sid, body):
        secs, _ = self.sections(sid)
        s = next((x for x in secs if x['id'] == body.get('section')), None)
        if not s:
            raise ValueError('Неизвестная секция')
        chosen = body.get('selected')
        if not isinstance(chosen, list) or not set(chosen) <= set(s['files']):
            raise ValueError('Неверный набор кадров')
        if s['limit'] and len(chosen) > s['limit']:
            raise ValueError(f'Максимум {s["limit"]} кадров')
        approve = bool(body.get('approved'))
        if any(p not in chosen for p in s['mandatory']):
            raise ValueError('Обязательные кадры из ведомости нельзя снять. Если кадр неверный — исправьте ведомость.')
        with self.conn() as c:
            before = {r['file_path'] for r in c.execute('SELECT file_path FROM selections WHERE session_id=? AND section_id=?', (sid, s['id']))}
            after = set(chosen)
            for p in after - before:
                c.execute('INSERT INTO selection_events(session_id,file_path,actor,action,section_id,at) VALUES(?,?,?,?,?,?)', (sid, p, 'user', 'select', s['id'], now()))
            for p in before - after:
                c.execute('INSERT INTO selection_events(session_id,file_path,actor,action,section_id,at) VALUES(?,?,?,?,?,?)', (sid, p, 'user', 'deselect', s['id'], now()))
            c.execute('DELETE FROM selections WHERE session_id=? AND section_id=?', (sid, s['id']))
            c.executemany('INSERT INTO selections VALUES(?,?,?)', [(sid, s['id'], p) for p in after])
            c.execute('INSERT OR REPLACE INTO approvals(session_id,section_id,approved,at) VALUES(?,?,?,?)', (sid, s['id'], int(approve), now()))
            self.event(c, sid, 'review', ('approve:' if approve else 'save:') + s['id'])
            if approve:
                total = len(secs)
                done = c.execute('SELECT COUNT(*) FROM approvals WHERE session_id=? AND approved=1', (sid,)).fetchone()[0]
                if done >= total:
                    c.execute("UPDATE sessions SET status='reviewed' WHERE id=? AND status!='exported'", (sid,))
        return {'ok': True}

    def set_role(self, sid, body):
        """Fix a worksheet mistake inside the app.
        role=None on a mandatory frame: 'not this person' -> the role becomes empty and the frame is unselected.
        role='V'/'P1'/...: this frame takes the role; the frame that had it is unselected.
        reset=True: forget all fixes of this section and use the worksheet again."""
        secs, _ = self.sections(sid)
        s = next((x for x in secs if x['id'] == body.get('section')), None)
        if not s or not s['person'] or not s['roles']:
            raise ValueError('Роли можно менять только у учеников и учителей из ведомости')
        path, role = body.get('path'), body.get('role')
        with self.conn() as c:
            if body.get('reset'):
                c.executemany('DELETE FROM anchor_overrides WHERE session_id=? AND person=? AND key=?',
                              [(sid, s['person'], k) for k in s['roles']])
                self.event(c, sid, 'review', 'reset-roles:' + s['id'])
            else:
                if path not in s['files']:
                    raise ValueError('Кадр не из этой секции')
                if role is None:
                    key = s['mandatory'].get(path)
                    if not key:
                        raise ValueError('Этот кадр не обязательный — его можно просто снять кликом')
                    c.execute('INSERT OR REPLACE INTO anchor_overrides(session_id,person,key,file_path,at) VALUES(?,?,?,?,?)',
                              (sid, s['person'], key, '', now()))
                    drop = [path]
                else:
                    if role not in s['roles']:
                        raise ValueError('Эта роль не относится к секции')
                    drop = [p for p, k in s['mandatory'].items() if k == role and p != path]
                    # the frame loses any other role it had in this section
                    old = s['mandatory'].get(path)
                    if old and old != role:
                        c.execute('INSERT OR REPLACE INTO anchor_overrides(session_id,person,key,file_path,at) VALUES(?,?,?,?,?)',
                                  (sid, s['person'], old, '', now()))
                    c.execute('INSERT OR REPLACE INTO anchor_overrides(session_id,person,key,file_path,at) VALUES(?,?,?,?,?)',
                              (sid, s['person'], role, path, now()))
                    c.execute('INSERT OR IGNORE INTO selections VALUES(?,?,?)', (sid, s['id'], path))
                    c.execute('INSERT INTO selection_events(session_id,file_path,actor,action,section_id,at) VALUES(?,?,?,?,?,?)',
                              (sid, path, 'user', 'role:' + role, s['id'], now()))
                for p in drop:
                    c.execute('DELETE FROM selections WHERE session_id=? AND section_id=? AND file_path=?', (sid, s['id'], p))
                    c.execute('INSERT INTO selection_events(session_id,file_path,actor,action,section_id,at) VALUES(?,?,?,?,?,?)',
                              (sid, p, 'user', 'not-this-person', s['id'], now()))
                self.event(c, sid, 'review', f'role:{s["id"]}:{role}')
            c.execute('DELETE FROM approvals WHERE session_id=? AND section_id=?', (sid, s['id']))
        return {'ok': True}

    def set_meta(self, sid, body):
        meta = {}
        with self.conn() as c:
            r = c.execute('SELECT meta FROM sessions WHERE id=?', (sid,)).fetchone()
            if not r:
                raise ValueError('Съёмка не найдена')
            meta = json.loads(r['meta'] or '{}')
            if 'background' in body:
                bg = str(body['background']).strip().strip('"')
                if bg and not Path(bg).is_file():
                    raise ValueError('Файл фона не найден')
                meta['background'] = bg
            if 'last_export' in body:
                meta['last_export'] = str(body['last_export'])
            if 'output' in body:
                meta['output'] = str(body['output']).strip().strip('"')
            c.execute('UPDATE sessions SET meta=? WHERE id=?', (json.dumps(meta, ensure_ascii=False), sid))
        return meta

    # -------------------------------------------------------- export
    def plan_export(self, sid, output):
        st = self.session(sid)
        s = st['session']
        src = Path(s['source']).resolve()
        out = Path(str(output or '').strip().strip('"')).expanduser().resolve()
        if not str(output or '').strip():
            raise ValueError('Укажите папку для экспорта')
        if out == src or src in out.parents:
            raise ValueError('Папка экспорта не может быть внутри папки исходников')
        if st['conflicts']:
            raise ValueError('Сначала исправьте ведомость: ' + '; '.join(st['conflicts'][:3]))
        missing = [x['title'] + ' — ' + x['subtitle'] for x in st['sections'] if not st['approved'].get(x['id'])]
        if missing:
            raise ValueError(f'Не подтверждено секций: {len(missing)} (например: {missing[0]})')
        files = {f['path']: f for f in st['files']}
        ops, names = [], set()

        def add(p, rel, who='', what='', role=''):
            if str(rel).casefold() in names:
                raise ValueError('Конфликт имён: ' + str(rel))
            names.add(str(rel).casefold())
            ops.append((p, rel, who, what, role))
        if s['mode'] == 'school':
            root = Path(*[safe_name(x or '—') for x in (s['order_no'], s['school'], s['shoot_date'], s['class_name'])])
            prefix = {'V': '{n}_', 'P1': 'ЛР_{n}_', 'P2': 'ЛС_{n}_', 'D1': 'Друг1_', 'D2': 'Друг2_', 'T': ''}
            # portraits and friends of one student are merged: a frame inside both windows is copied once
            by_person, subs = defaultdict(set), {}
            mand, roles = {}, {}
            for sec in st['sections']:
                key = (sec['person'], sec['kind'] == 'teacher', sec['title'])
                subs[key] = sec['subtitle']
                for p in [x for x in st['selected'].get(sec['id'], []) if x in sec['files'] or x in sec['mandatory']]:
                    by_person[key].add(p)
                for p, k in sec['mandatory'].items():
                    mand[(sec['person'], p)] = prefix[k].format(n=sec['title'])
                    roles[(sec['person'], p)] = ROLE_NAMES.get(k, k)
            for (person, is_teacher, name), paths in by_person.items():
                sub = subs[(person, is_teacher, name)]
                for p in sorted(paths):
                    nm = files[p]['name']
                    if is_teacher:
                        rel = root / CATS[5] / (nm if len(paths) > 1 else safe_name(name if sub == 'Учитель' else f'{name} {sub}') + Path(nm).suffix)
                    else:
                        rel = root / CATS[4] / name / (mand.get((person, p), '') + nm)
                    add(p, rel, name, 'Учитель' if is_teacher else 'Ученик', roles.get((person, p), ''))
            bg = json.loads(s['meta'] or '{}').get('background')
            if bg:
                add(bg, root / CATS[6] / Path(bg).name, '', 'Фон')
        else:
            root = Path(safe_name(s['title']))
            for sec in st['sections']:
                for p in [x for x in st['selected'].get(sec['id'], []) if x in sec['files'] or x in sec['mandatory']]:
                    add(p, root / safe_name(sec['title']) / files[p]['name'], sec['title'], 'Серия')
        if not ops:
            raise ValueError('Не выбрано ни одного кадра')
        if (out / root).exists() and any((out / root).iterdir()):
            raise ValueError('Папка заказа уже существует и не пуста: ' + str(out / root))
        need = sum(Path(op[0]).stat().st_size for op in ops)
        free = shutil.disk_usage(out if out.exists() else out.parent if out.parent.exists() else Path.home()).free
        return {'root': str(out / root), 'files': len(ops), 'bytes': need, 'free': free, 'ops': ops, 'out': out,
                'categories': s['mode'] == 'school', 'rootrel': root}

    def export(self, sid, output, dry_run=False, job=None):
        job = job if job is not None else {}
        plan = self.plan_export(sid, output)
        if plan['bytes'] > plan['free']:
            raise ValueError('Недостаточно места на диске')
        if dry_run:
            return {k: plan[k] for k in ('root', 'files', 'bytes', 'free')}
        with self.conn() as c:
            self.event(c, sid, 'export', 'start')
        root = plan['out'] / plan['rootrel']
        job.update(stage='Копирование', total=len(plan['ops']), done=0, bytes_total=plan['bytes'], bytes_done=0)
        report = []
        for p, rel, who, what, role in plan['ops']:
            d = plan['out'] / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            if d.exists():
                raise ValueError('Файл уже существует, перезапись запрещена: ' + str(d))
            shutil.copy2(p, d)
            size = d.stat().st_size
            if size != Path(p).stat().st_size:
                raise ValueError('Копия не совпадает по размеру с исходником: ' + str(d))
            report.append([what, who, role, str(rel.relative_to(plan['rootrel'])), str(p), size])
            job['done'] += 1
            job['bytes_done'] += size
        if plan['categories']:
            for cat in CATS:
                (root / cat).mkdir(parents=True, exist_ok=True)
        # order manifest: opens in Russian Excel as a table (UTF-8 BOM, ';')
        with open(root / '_FOBOX_отчёт.csv', 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(['Тип', 'Кто', 'Роль', 'Файл в заказе', 'Исходный файл', 'Размер, байт'])
            w.writerows(sorted(report, key=lambda r: (r[0], r[1], r[3])))
        with self.conn() as c:
            self.event(c, sid, 'export', 'end')
            c.execute("UPDATE sessions SET status='exported' WHERE id=?", (sid,))
        self.set_meta(sid, {'last_export': str(root)})
        job['stage'] = 'Готово'
        return {'root': plan['root'], 'files': plan['files'], 'bytes': plan['bytes'], 'report': str(root / '_FOBOX_отчёт.csv')}

    def start_export(self, sid, output):
        self.plan_export(sid, output)  # validate synchronously so the dialog shows errors at once
        jid = sid + ':export'
        job = {'session': sid, 'stage': 'Подготовка', 'done': 0, 'total': 0, 'error': None, 'finished': False}
        self.jobs[jid] = job

        def run():
            try:
                job['result'] = self.export(sid, output, job=job)
            except Exception as e:
                job['error'] = str(e)
            job['finished'] = True
        threading.Thread(target=run, daemon=True).start()
        return {'job': jid}

    def delete_session(self, sid):
        """Removes the session from the catalog only. Source files are not touched."""
        with self.conn() as c:
            for t in ('files', 'students', 'selection_events', 'workflow_events', 'ai_suggestions', 'selections', 'approvals', 'anchor_overrides'):
                c.execute(f'DELETE FROM {t} WHERE session_id=?', (sid,))
            c.execute('DELETE FROM sessions WHERE id=?', (sid,))

    # -------------------------------------------------------- statistics
    def stats(self):
        with self.conn() as c:
            sessions = [dict(r) for r in c.execute('SELECT * FROM sessions ORDER BY first_capture DESC')]
            per_day = [dict(r) for r in c.execute('SELECT substr(capture_at,1,10) d, COUNT(*) n, session_id FROM files GROUP BY d, session_id')]
            flags = Counter()
            analyzed = 0
            for r in c.execute('SELECT flags, analyzed FROM files'):
                analyzed += r['analyzed'] or 0
                for fl in (r['flags'] or '').split(','):
                    if fl:
                        flags[fl] += 1
            nfiles = c.execute('SELECT COUNT(*) FROM files').fetchone()[0]
            nraw = c.execute("SELECT COUNT(*) FROM files WHERE kind='raw'").fetchone()[0]
            sugg = c.execute('SELECT COUNT(*) FROM ai_suggestions').fetchone()[0]
            # suggestions still selected in approved sections = accepted
            accepted = c.execute('''SELECT COUNT(*) FROM ai_suggestions a JOIN selections s USING(session_id,section_id,file_path)
                                    JOIN approvals p ON p.session_id=a.session_id AND p.section_id=a.section_id AND p.approved=1''').fetchone()[0]
            rejected = c.execute('''SELECT COUNT(*) FROM ai_suggestions a JOIN approvals p ON p.session_id=a.session_id
                                    AND p.section_id=a.section_id AND p.approved=1
                                    WHERE NOT EXISTS(SELECT 1 FROM selections s WHERE s.session_id=a.session_id
                                    AND s.section_id=a.section_id AND s.file_path=a.file_path)''').fetchone()[0]
            manual_add = c.execute('''SELECT COUNT(*) FROM selections s JOIN approvals p ON p.session_id=s.session_id
                                      AND p.section_id=s.section_id AND p.approved=1
                                      WHERE s.file_path NOT IN (SELECT file_path FROM ai_suggestions a WHERE a.session_id=s.session_id AND a.section_id=s.section_id)
                                      AND NOT EXISTS(SELECT 1 FROM selection_events e WHERE e.session_id=s.session_id AND e.section_id=s.section_id
                                      AND e.file_path=s.file_path AND e.actor='worksheet')''').fetchone()[0]
            approved_secs = c.execute('SELECT session_id, section_id FROM approvals WHERE approved=1').fetchall()
            untouched = 0
            for a in approved_secs:
                ev = c.execute("SELECT COUNT(*) FROM selection_events WHERE session_id=? AND section_id=? AND actor='user'", (a[0], a[1])).fetchone()[0]
                untouched += ev == 0
            wf = [dict(r) for r in c.execute('SELECT session_id,stage,event,at FROM workflow_events ORDER BY id')]
            people = {r['session_id']: r['n'] for r in c.execute("SELECT session_id, COUNT(*) n FROM students WHERE role='student' GROUP BY session_id")}
            kits = c.execute('''SELECT COUNT(*) FROM students st WHERE role='student' AND
                                (SELECT COUNT(*) FROM approvals a WHERE a.session_id=st.session_id AND a.approved=1
                                 AND a.section_id IN (st.student_id||'p', st.student_id||'f'))=2''').fetchone()[0]
        fmt = '%Y-%m-%d %H:%M:%S'
        for s in sessions:
            s['frames'] = sum(d['n'] for d in per_day if d['session_id'] == s['id'])
            s['students'] = people.get(s['id'], 0)
            s['hours'] = None
            if s['first_capture'] and s['last_capture']:
                s['hours'] = round((datetime.strptime(s['last_capture'], fmt) - datetime.strptime(s['first_capture'], fmt)).total_seconds() / 3600, 2)
        days = defaultdict(int)
        for d in per_day:
            days[d['d']] += d['n']
        months = defaultdict(lambda: {'frames': 0, 'sessions': 0})
        for d, n in days.items():
            months[d[:7]]['frames'] += n
        for s in sessions:
            if s['shoot_date']:
                months[s['shoot_date'][:7]]['sessions'] += 1
        # workflow durations (pairs start/end per stage)
        dur = defaultdict(list)
        opened = {}
        for e in wf:
            k = (e['session_id'], e['stage'])
            if e['event'] == 'start':
                opened[k] = e['at']
            elif e['event'] == 'end' and k in opened:
                dur[e['stage']].append((datetime.strptime(e['at'], fmt) - datetime.strptime(opened.pop(k), fmt)).total_seconds())
        review_times = defaultdict(list)
        for e in wf:
            if e['stage'] == 'review' and e['event'].startswith('approve:'):
                review_times[e['session_id']].append(datetime.strptime(e['at'], fmt))
        per_section = []
        for ts in review_times.values():
            ts.sort()
            per_section += [(b - a).total_seconds() for a, b in zip(ts, ts[1:]) if (b - a).total_seconds() < 1800]
        records = {}
        with_hours = [s for s in sessions if s['hours'] is not None]
        if with_hours:
            x = max(with_hours, key=lambda s: s['hours'])
            records['longest'] = {'value': x['hours'], 'title': x['title'], 'date': x['shoot_date']}
        if days:
            d = max(days, key=days.get)
            records['max_day'] = {'value': days[d], 'date': d}
        if months:
            m = max(months, key=lambda k: months[k]['frames'])
            records['active_month'] = {'value': months[m]['frames'], 'month': m}
        if people:
            sid = max(people, key=people.get)
            t = next((s for s in sessions if s['id'] == sid), None)
            if t:
                records['biggest_class'] = {'value': people[sid], 'title': t['title'], 'date': t['shoot_date']}
        total_hours = sum(s['hours'] for s in with_hours) if with_hours else None
        avg = lambda v: round(sum(v) / len(v), 1) if v else None
        return {
            'sessions': sessions, 'files': nfiles, 'raw': nraw, 'analyzed': analyzed, 'flags': dict(flags),
            'hours': round(total_hours, 1) if total_hours is not None else None, 'kits': kits,
            'days': dict(days), 'months': {k: months[k] for k in sorted(months)},
            'selection': {'suggested': sugg, 'accepted': accepted, 'replaced': rejected, 'manual_added': manual_add,
                          'sections_untouched': untouched, 'sections_approved': len(approved_secs)},
            'workflow': {'import_sec': avg(dur['import']), 'analysis_sec': avg(dur['analysis']),
                         'export_sec': avg(dur['export']), 'review_per_section_sec': avg(per_section)},
            'records': records, 'pillow': Image is not None,
        }
