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
import os
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
    'target_per_student': '18',    # school mode: kit size goal incl. the 5 worksheet frames
    'group_dups': '6',             # common class photo: 1 main frame + this many duplicates (5–7)
    'report_count': '15',          # reportage: frames in the order
    'report_dups': '6',            # reportage: extra duplicates (5–7)
    'kit_min': '15',               # automatic kit: at least this many frames when the series has them
    'kit_max': '20',               # automatic kit: never more than this (15–20 incl. the 5 worksheet frames)
    'blur_threshold': '20',        # strongest-edge level below -> "blur"
    'dup_distance': '5',           # dHash Hamming distance for near-duplicates
    'clip_threshold': '0.25',      # share of clipped pixels for over/under
    'accent': '#7738FF',
    'theme': 'dark',
    'workspace': 'Моя студия',
    'teacher_gap_sec': '30',       # teacher series ends at a longer pause between frames
    'person_gap_sec': '45',        # a child's series ends at a longer pause between frames
    'date_format': 'dd.mm.yyyy',   # how dates are shown in the interface
    'compare_to': 'prev',          # overview comparison: previous period or same period last year
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
CREATE TABLE IF NOT EXISTS marks (
 session_id TEXT NOT NULL, section_id TEXT NOT NULL, file_path TEXT NOT NULL, mark TEXT NOT NULL,
 PRIMARY KEY(session_id, section_id, file_path));
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
    'profiles': {'email': 'TEXT', 'phone': 'TEXT', 'city': 'TEXT', 'avatar': 'TEXT', 'site': 'TEXT',
                 'telegram': 'TEXT', 'vk': 'TEXT', 'instagram': 'TEXT', 'youtube': 'TEXT', 'role': 'TEXT'},
}
SCHEMA_VERSION = 2


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def frame_number(stem):
    m = re.search(r'(\d+)$', stem)
    return int(m.group(1)) if m else None


def iso_date(v):
    """'16.10.2026', '16/10/2026', '2026-10-16' -> '2026-10-16'; anything else unchanged."""
    v = str(v or '').strip()
    m = re.fullmatch(r'(\d{1,2})[./-](\d{1,2})[./-](\d{4})', v)
    if m:
        return f'{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}'
    m = re.fullmatch(r'(\d{4})[./-](\d{1,2})[./-](\d{1,2})', v)
    return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}' if m else v


def ru_date(v):
    m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', str(v or ''))
    return f'{m.group(3)}.{m.group(2)}.{m.group(1)}' if m else str(v or '')


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
    return re.sub(r'[\s._\-№#:,;/]+', '', h.strip().casefold()).replace('ё', 'е')


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


SUBJECTS = {
    'физкультура': 'физкультуры', 'физическаякультура': 'физической культуры', 'математика': 'математики',
    'алгебра': 'алгебры', 'геометрия': 'геометрии', 'физика': 'физики', 'химия': 'химии', 'биология': 'биологии',
    'история': 'истории', 'литература': 'литературы', 'чтение': 'чтения', 'литературноечтение': 'литературного чтения',
    'география': 'географии', 'информатика': 'информатики', 'музыка': 'музыки', 'изо': 'ИЗО', 'технология': 'технологии',
    'труд': 'труда', 'обществознание': 'обществознания', 'обж': 'ОБЖ', 'астрономия': 'астрономии', 'экономика': 'экономики',
    'право': 'права', 'окружающиймир': 'окружающего мира', 'начальныеклассы': 'начальных классов', 'ритмика': 'ритмики',
    'хореография': 'хореографии', 'черчение': 'черчения', 'мхк': 'МХК', 'естествознание': 'естествознания',
}


def teacher_title(role):
    """'Физкультура' -> 'учитель физкультуры', 'Английский язык' -> 'учитель английского языка';
    a position ('Логопед', 'Классный руководитель') is kept, written in lower case."""
    r = ' '.join(str(role or '').split())
    if not r:
        return ''
    low = r[0].lower() + r[1:] if not r[:2].isupper() else r
    n = _norm(r)
    if n.startswith(('учитель', 'преподаватель', 'педагог')):
        return low
    if n in SUBJECTS:
        return 'учитель ' + SUBJECTS[n]
    m = re.fullmatch(r'(?i)([а-яё]+)(ий|ый|ой)\s+язык', r)
    if m:  # английский язык -> английского языка
        return f'учитель {m.group(1).lower()}ого языка'
    return low


def _col_index(ref):
    col = 0
    for ch in re.match(r'([A-Z]+)', ref)[1]:
        col = col * 26 + ord(ch) - 64
    return col - 1


def _xlsx_book(path):
    """All sheets of an .xlsx as [(sheet name, rows, comments)] without external libraries.
    rows[i] is Excel row i+1 (empty rows kept, so comments can be matched); comments: {(row, col): text}."""
    import posixpath
    import zipfile
    import xml.etree.ElementTree as ET
    M = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    ns = {'m': M}
    rid = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'

    def rels_of(z, part):
        d, b = posixpath.split(part)
        rp = posixpath.join(d, '_rels', b + '.rels')
        if rp not in z.namelist():
            return {}
        return {r.get('Id'): (r.get('Type', ''), posixpath.normpath(posixpath.join(d, r.get('Target'))) if not r.get('Target', '').startswith('/') else r.get('Target').lstrip('/'))
                for r in ET.fromstring(z.read(rp))}
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', ns):
                shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % M)))
        wbrels = rels_of(z, 'xl/workbook.xml')
        sheets = []
        for sh in ET.fromstring(z.read('xl/workbook.xml')).iterfind('m:sheets/m:sheet', ns):
            part = wbrels.get(sh.get(rid), ('', ''))[1]
            if not part or part not in z.namelist():
                continue
            rows = {}
            for row in ET.fromstring(z.read(part)).iter('{%s}row' % M):
                vals = {}
                for cell in row.findall('m:c', ns):
                    typ, v = cell.get('t'), cell.find('m:v', ns)
                    if typ == 's' and v is not None:
                        val = shared[int(v.text)]
                    elif typ == 'inlineStr':
                        val = ''.join(t.text or '' for t in cell.iter('{%s}t' % M))
                    else:
                        val = v.text if v is not None else ''
                    vals[_col_index(cell.get('r', 'A'))] = val
                if vals:
                    rows[int(row.get('r')) - 1] = [vals.get(k, '') for k in range(max(vals) + 1)]
            dense = [rows.get(k, []) for k in range(max(rows) + 1)] if rows else []
            comments = {}
            for typ, target in rels_of(z, part).values():
                if typ.endswith('/comments') and target in z.namelist():
                    for cm in ET.fromstring(z.read(target)).iter('{%s}comment' % M):
                        ref = cm.get('ref', 'A1')
                        txt = ''.join(t.text or '' for t in cm.iter('{%s}t' % M)).strip()
                        comments[(int(re.search(r'(\d+)', ref)[1]) - 1, _col_index(ref))] = txt
            sheets.append((sh.get('name', ''), dense, comments))
        return sheets


def _xlsx_rows(path):
    book = _xlsx_book(path)
    return book[0][1] if book else []


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


META_LABELS = {'order': ['договор', 'заказ', 'номерзаказа'], 'school': ['учебноезаведение', 'школа', 'учреждение', 'заведение', 'садик', 'детскийсад'],
               'class_name': ['класс', 'группа', 'классгруппа'], 'date': ['дата', 'датасъемки']}
SKIP_ROW = re.compile(r'(?i)(договор|заведени|учрежден|класс|групп|дата|школа|заказ|ведомост|выделен|итого|примечан)')
TEACHER_COLS2 = {'Name': ['name', 'фио', 'учитель', 'педагог', 'фамилияимя', 'фамилияимяотчество', 'имя'],
                 'T': ['фото', 'фотоучителя', 'кадр', 'кадры', 'номер', 'номера', 'снимок', 'фотография', 'номеркадра', 'портрет', 'номерпортрета']}
TEACHER_OPT = {'Note': ['примечание', 'комментарий', 'заметка', 'note'], 'Role': ['role', 'должность', 'роль', 'предмет', 'дисциплина'], 'Start': ['start', 'начало', 'первыйкадр', 'от'],
               'End': ['end', 'конец', 'последнийкадр', 'до']}


def _load_rows(p):
    """[(sheet name, rows, comments)] for .xlsx/.csv; a CSV pasted into one Excel column is split."""
    ext = p.suffix.lower()
    if ext in ('.xlsx', '.xlsm'):
        book = _xlsx_book(p)
    elif ext == '.xls':
        raise ValueError('Старый формат .xls: откройте файл в Excel и сохраните как .xlsx')
    else:
        book = [('', _csv_rows(p), {})]
    out = []
    for name, rows, comments in book:
        filled = [r for r in rows if any(str(x).strip() for x in r)]
        if filled and sum(1 for r in filled if len([x for x in r if str(x).strip()]) == 1 and ';' in str(next(x for x in r if str(x).strip()))) > len(filled) * 0.6:
            rows = [str(next((x for x in r if str(x).strip()), '')).split(';') for r in rows]
        out.append((name, rows, comments))
    return out


def _sheet_meta(rows):
    meta = {}
    for r in rows[:12]:
        cells = [str(x).strip() for x in r]
        for i, c in enumerate(cells):
            n = _norm(c)
            for key, labels in META_LABELS.items():
                if key not in meta and n and any(n == l or n.startswith(l) for l in labels):
                    val = next((v for v in cells[i + 1:] if v), '')
                    if val and not _norm(val) in sum(META_LABELS.values(), []):
                        meta[key] = _cell(val)
    return meta


def _parse_rows(rows, columns, optional=None, comments=None, positional=None):
    """Records from one sheet. Header row is searched in the first 15 rows; without it, the layout is
    taken by position (name, then the frame numbers in `positional` order)."""
    optional, comments = optional or {}, comments or {}
    alias = {_norm(a): key for key, names in {**columns, **optional}.items() for a in names + [key]}
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
        for ri in range(hi + 1, len(rows)):
            row = rows[ri]
            rec = {key: _cell(row[i]) if i < len(row) else '' for i, key in mapping.items()}
            if rec.get('Name') and not SKIP_ROW.match(rec['Name']):
                unsure = {key: comments[(ri, i)] for i, key in mapping.items() if (ri, i) in comments}
                if unsure:
                    rec['_unsure'] = unsure
                out.append(rec)
        return out
    if not positional:
        return None
    for ri, row in enumerate(rows):
        cells = [_cell(x) for x in row]
        idx = next((i for i, v in enumerate(cells) if _looks_like_name(v) and not re.match(r'(?i)(ученик|учител|тип|фамил|фио|name|№)', v)), None)
        if idx is None or SKIP_ROW.match(cells[idx]):
            continue
        rest = cells[idx + 1:]
        if not any(re.fullmatch(r'[A-Za-z_]*\d+[\w.]*(\s+[A-Za-z_]*\d+[\w.]*)*', v) for v in rest[:len(positional)]):
            continue  # a title or label row, not a person
        rec = {'Name': cells[idx], 'Type': next((v for v in cells[:idx] if v and not v.isdigit()), '')}
        for k, v in zip(positional, rest):
            rec[k] = v if v not in ('—', '-', '–') else ''
        unsure = {k: comments[(ri, idx + 1 + n)] for n, k in enumerate(positional) if (ri, idx + 1 + n) in comments}
        if (ri, idx) in comments:
            unsure['Name'] = comments[(ri, idx)]
        if unsure:
            rec['_unsure'] = unsure
        out.append(rec)
    return out


def read_worksheet(path, columns, optional=None):
    """Read the first sheet of an Excel (.xlsx) or CSV worksheet (kept for compatibility)."""
    p = Path(str(path).strip().strip('"')).expanduser()
    if not p.is_file():
        raise ValueError('Не найдена ведомость: ' + str(p))
    name, rows, comments = _load_rows(p)[0]
    out = _parse_rows(rows, columns, optional, comments, ['V', 'P1', 'P2', 'D1', 'D2', 'T'] if 'V' in columns else None)
    if out is None:
        expected = ', '.join(names[1] if len(names) > 1 else names[0] for names in columns.values())
        raise ValueError(f'{p.name}: не найдена строка заголовков. Нужны колонки: {expected}')
    if not out:
        raise ValueError(f'{p.name}: не удалось прочитать. Нужно: Фамилия Имя и 5 номеров (Виньетка, ЛР, ЛС, Друг 1, Друг 2)')
    return out


def read_workbook(path):
    """The whole worksheet: students (first non-teacher sheet), teachers (sheet named «Учителя…»
    or rows typed «учителя»), and header facts like contract number, school and class."""
    p = Path(str(path).strip().strip('"')).expanduser()
    if not p.is_file():
        raise ValueError('Не найдена ведомость: ' + str(p))
    book = _load_rows(p)
    students, teachers, meta = [], [], {}
    for name, rows, comments in book:
        is_teacher_sheet = bool(re.match(r'(?i)\s*(учит|педаг|teach)', name))
        if is_teacher_sheet:
            for k, v in _sheet_meta(rows).items():
                meta.setdefault(k, v)
            recs = _parse_rows(rows, TEACHER_COLS2, TEACHER_OPT, comments)
            if recs is None:
                recs = _parse_rows(rows, {'Name': TEACHER_COLS2['Name'], 'Start': TEACHER_OPT['Start']}, {'End': TEACHER_OPT['End'], 'Role': TEACHER_OPT['Role'], 'Note': TEACHER_OPT['Note']}, comments)
            if recs is None:
                recs = _parse_rows(rows, {'Name': ['__none__']}, None, comments, ['T'])
            for r in recs or []:
                r['Type'] = 'учителя'
                if r.get('Note'):  # a remark in the worksheet is something to check
                    r.setdefault('_unsure', {})['Note'] = r['Note']
            teachers += recs or []
            continue
        if students:
            continue  # only the first students sheet is used
        meta.update(_sheet_meta(rows))
        recs = _parse_rows(rows, STUDENT_COLS, EXTRA_COLS, comments, ['V', 'P1', 'P2', 'D1', 'D2', 'T'])
        students = recs or []
    if not students and not teachers:
        raise ValueError(f'{p.name}: не удалось прочитать. Нужно: Фамилия Имя и 5 номеров (Виньетка, ЛР, ЛС, Друг 1, Друг 2)')
    return {'students': students, 'teachers': teachers, 'meta': meta, 'sheets': [b[0] for b in book]}


def worksheet_summary(path):
    """What the import dialog shows right after a worksheet is chosen."""
    wb = read_workbook(path)
    st = [r for r in wb['students'] if not str(r.get('Type', '')).casefold().startswith(('учит', 'педаг'))]
    te = wb['teachers'] + [r for r in wb['students'] if str(r.get('Type', '')).casefold().startswith(('учит', 'педаг'))]
    unsure = sum(len(r.get('_unsure', {})) for r in wb['students'] + wb['teachers'])
    empty = sum(1 for r in st if not any(r.get(k) for k in ('V', 'P1', 'P2', 'D1', 'D2')))
    return {'students': len(st), 'teachers': len(te), 'unsure': unsure, 'no_numbers': empty, 'meta': wb['meta'],
            'sheets': wb['sheets'], 'sample': [r['Name'] for r in st[:3]]}


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
            c.execute("UPDATE settings SET value='20' WHERE key='kit_max' AND value='22'")   # earlier default
            for r in c.execute('SELECT id, shoot_date FROM sessions').fetchall():   # older catalogs: 16.10.2026 -> ISO
                if r['shoot_date'] and iso_date(r['shoot_date']) != r['shoot_date']:
                    c.execute('UPDATE sessions SET shoot_date=? WHERE id=?', (iso_date(r['shoot_date']), r['id']))

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
                if k in ('series_gap_sec', 'teacher_gap_sec', 'person_gap_sec', 'radius', 'target_per_student', 'kit_min', 'kit_max', 'group_dups', 'report_count', 'report_dups', 'dup_distance'):
                    if not str(v).isdigit() or int(v) <= 0:
                        raise ValueError('Положительное целое число: ' + k)
                c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (k, str(v)))

    def profile(self):
        with self.conn() as c:
            return dict(c.execute("SELECT * FROM profiles WHERE id='me'").fetchone())

    def save_profile(self, data):
        fields = ['name', 'studio', 'email', 'phone', 'city', 'site', 'telegram', 'vk', 'instagram', 'youtube', 'role']
        fields = [f for f in fields if f in data]
        with self.conn() as c:
            if fields:
                c.execute('UPDATE profiles SET ' + ','.join(f'{f}=?' for f in fields) + " WHERE id='me'",
                          [str(data.get(f, ''))[:200] for f in fields])
            if 'avatar' in data:
                av = str(data['avatar'] or '')
                if av and (not av.startswith('data:image/') or len(av) > 400_000):
                    raise ValueError('Фото профиля: изображение до 300 КБ')
                c.execute("UPDATE profiles SET avatar=? WHERE id='me'", (av,))

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
        if mode == 'school':
            # contract number, school and class from the worksheet header fill empty fields
            meta = read_workbook(opts['students'])['meta']
            opts = {**opts, **{k: v for k, v in meta.items() if not str(opts.get(k) or '').strip()}}
            if not (opts.get('title') or '').strip() and (opts.get('school') or opts.get('class_name')):
                opts['title'] = ', '.join(x for x in (opts.get('school'), opts.get('class_name')) if x)
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
                      (span['a'], span['b'], iso_date(opts.get('date')) or (span['a'] or '')[:10], sid))
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
        wb = read_workbook(students_file) if students_file else {'students': [], 'teachers': []}
        for r in wb['students'] + wb['teachers']:
            typ = (r.get('Type') or '').casefold()
            unsure = r.get('_unsure') or {}
            is_teacher = typ.startswith(('учит', 'педаг', 'teach')) or \
                (r.get('T') and not any(r.get(k) for k in ('V', 'P1', 'P2', 'D1', 'D2')))
            if is_teacher:
                if r.get('Start') and not r.get('T'):
                    anchors = {'Role': r.get('Role', ''), 'Start': r.get('Start', ''), 'End': r.get('End', '')}
                else:
                    anchors = {'Role': r.get('Role', ''), 'T': r.get('T') or ' '.join(r.get(k, '') for k in ('V', 'P1', 'P2', 'D1', 'D2')).strip()}
                anchors['unsure'] = unsure
                c.execute('INSERT INTO students(session_id,student_id,name,role,anchors) VALUES(?,?,?,?,?)',
                          (sid, f't{nt}', safe_name(' '.join(r['Name'].split())), 'teacher', json.dumps(anchors, ensure_ascii=False)))
                nt += 1
                continue
            if typ.startswith(('общ', 'груп', 'репорт')):
                nums = ' '.join(r.get(k, '') for k in ('V', 'P1', 'P2', 'D1', 'D2', 'T')).strip()
                kind = 'report' if typ.startswith('репорт') else 'group'
                c.execute('INSERT OR REPLACE INTO students(session_id,student_id,name,role,anchors) VALUES(?,?,?,?,?)',
                          (sid, 'x' + kind, r['Name'], kind, json.dumps({'nums': nums}, ensure_ascii=False)))
                continue
            if typ and not typ.startswith(('учен', 'дет', 'реб', 'stud', 'kid')):
                continue  # other categories (документы …) are not personal pages
            anchors = {k: r.get(k, '') for k in ('V', 'P1', 'P2', 'D1', 'D2')}
            anchors['unsure'] = unsure
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

    def _common_sections(self, files, frames, people, out, conflicts):
        """«Общая фотография» and «Репортаж»: frames that belong to nobody's series.
        Worksheet rows typed «общая» / «репортаж» may give numbers ('612', '640-700', '640 641')."""
        used = {p for x in out for p in x['files']} | {p for x in out for p in x['mandatory']}
        free = [f for f in frames if f['path'] not in used]
        sheet = {p['role']: json.loads(p['anchors'] or '{}').get('nums', '') for p in people if p['role'] in ('group', 'report')}
        fnum = {f['frame']: f for f in frames if f['frame'] is not None}

        def nums_of(text):
            out_ = []
            for a, b in re.findall(r'(\d+)\s*(?:[-–]\s*(\d+))?', text or ''):
                lo, hi = int(a), int(b or a)
                out_ += [fnum[n]['path'] for n in range(min(lo, hi), max(lo, hi) + 1) if n in fnum]
            return out_
        st = self.settings()
        res = []
        g_must = nums_of(sheet.get('group'))[:1]
        r_range = nums_of(sheet.get('report'))
        g_files = [f['path'] for f in free] if not g_must else sorted(set(g_must) | {f['path'] for f in free}, key=lambda p: next((f['capture_at'] for f in frames if f['path'] == p), ''))
        r_files = [p for p in r_range if p not in g_must] if r_range else [f['path'] for f in free]
        dg, dr = int(st['group_dups']), int(st['report_dups'])
        if g_files:
            res.append({'id': 'common', 'title': 'Общая фотография', 'subtitle': f'1 кадр + {dg} дублей', 'kind': 'group',
                        'person': None, 'files': g_files, 'mandatory': {p: 'G' for p in g_must}, 'limit': 1 + dg,
                        'main_limit': 1, 'dup_limit': dg, 'roles': [], 'overridden': False, 'unsure': {}})
        if r_files:
            n = int(st['report_count'])
            res.append({'id': 'report', 'title': 'Репортаж', 'subtitle': f'{n} кадров + {dr} дублей', 'kind': 'reportage',
                        'person': None, 'files': r_files, 'mandatory': {}, 'limit': n + dr,
                        'main_limit': n, 'dup_limit': dr, 'roles': [], 'overridden': False, 'unsure': {}})
        return res

    def _teacher_series(self, frames, own, anchors_all, gap=None, max_side=10):
        return self._series(frames, own, anchors_all, self.setting('teacher_gap_sec', int) if gap is None else gap, max_side)

    def _series(self, frames, own, anchors_all, gap, max_side):
        """Frames of one person's series: shot in a row around their worksheet frames, stopping at a pause
        longer than `gap` seconds or at a frame that belongs to someone else in the worksheet."""
        seq = sorted(frames, key=lambda f: (f['capture_at'], f['name']))
        idx = {f['path']: i for i, f in enumerate(seq)}
        t = lambda f: datetime.strptime(f['capture_at'], '%Y-%m-%d %H:%M:%S')
        keep = set()
        for p in own:
            if p not in idx:
                continue
            i0 = idx[p]
            keep.add(i0)
            for step in (-1, 1):
                i = i0
                while 0 <= i + step < len(seq) and abs(i + step - i0) <= max_side:
                    nxt = seq[i + step]
                    if nxt['path'] in anchors_all and nxt['path'] not in own:
                        break
                    if gap and abs((t(nxt) - t(seq[i])).total_seconds()) > gap:
                        break
                    i += step
                    keep.add(i)
        if len(keep) < 3 and gap and max_side > 3:
            # too short to choose poses from: show ±3 neighbours, still never crossing someone else's frame
            return self._series(frames, own, anchors_all, 0, 3) if any(p in idx for p in own) else []
        return [seq[i] for i in sorted(keep)]

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
            anchors_all, anchors_person = set(), set()   # every worksheet frame / portraits and teachers only
            for p in people:
                a = json.loads(p['anchors'] or '{}')
                anchors_all |= set(self._anchors(files, a, ['V', 'P1', 'P2', 'D1', 'D2', 'T'], [], p['name']))
                anchors_person |= set(self._anchors(files, a, ['V', 'P1', 'P2', 'T'], [], p['name']))
            pgap = self.setting('person_gap_sec', int)
            # portrait span of every person (first..last of V/P1/P2): a worksheet number of one child that
            # sits inside another child's span is almost always a misread number — never grow a series there
            fnum = {f['path']: f['frame'] for f in files}
            spans = {}
            for p in people:
                a = json.loads(p['anchors'] or '{}')
                nums = sorted(fnum[x] for x in self._anchors(files, a, ['V', 'P1', 'P2'], [], p['name']) if fnum.get(x) is not None)
                if len(nums) >= 2 and nums[-1] - nums[0] <= 40:
                    spans[p['student_id']] = (nums[0], nums[-1], p['name'], nums[len(nums) // 2])
            def intruder(pid, path):
                # suspicious: inside another child's span and closer to that child's middle number than to own
                n = fnum.get(path)
                if n is None:
                    return None
                mine = spans.get(pid)
                for oid, (lo, hi, oname, mid) in spans.items():
                    if oid != pid and lo < n < hi and (not mine or abs(n - mid) < abs(n - mine[3])):
                        return f'{oname} ({lo}–{hi})'
                return None
            # pass 1: portrait series of every child; a frame claimed by two children goes to the one
            # whose worksheet number is nearest — so a series never contains another child's frames
            pseries, pown = {}, {}
            for p in people:
                if p['role'] != 'student':
                    continue
                a = json.loads(p['anchors'] or '{}')
                cols = ['V', 'P1', 'P2']
                o = {k: v for k, v in ov[p['student_id']].items() if k in cols}
                own = set(self._anchors(files, a, cols, [], p['name'], o)) | set(self._anchors(files, a, cols, [], p['name']))
                grow = {x for x in own if not intruder(p['student_id'], x)}
                pseries[p['student_id']] = {f['path'] for f in (self._series(frames, grow, anchors_all, pgap, 25) if grow else [])}
                pown[p['student_id']] = [fnum[x] for x in own if fnum.get(x) is not None]
            claim = defaultdict(list)
            for pid, ps in pseries.items():
                for x in ps:
                    claim[x].append(pid)
            for x, pids in claim.items():
                if len(pids) > 1 and fnum.get(x) is not None:
                    best = min(pids, key=lambda q: min((abs(fnum[x] - n) for n in pown[q]), default=10 ** 9))
                    for q in pids:
                        if q != best:
                            pseries[q].discard(x)
            # never more than 4 frames past the outermost worksheet frame: without a pause between people
            # a long tail would reach the next person
            for pid, ps in pseries.items():
                ns = [n for n in pown[pid] if n is not None]
                if ns:
                    lo, hi = min(ns) - 4, max(ns) + 4
                    pseries[pid] = {x for x in ps if fnum.get(x) is None or lo <= fnum[x] <= hi}
            taken = {x: pid for pid, ps in pseries.items() for x in ps}
            for p in people:
                a = json.loads(p['anchors'] or '{}')
                if p['role'] == 'student':
                    for suffix, cols, label in (('p', ['V', 'P1', 'P2'], 'Портреты / виньетка'), ('f', ['D1', 'D2'], 'Друзья')):
                        o = {k: v for k, v in ov[p['student_id']].items() if k in cols}
                        must = self._anchors(files, a, cols, conflicts, p['name'], o)
                        # the window stays around the worksheet numbers even if a frame was reassigned
                        sheet = self._anchors(files, a, cols, [], p['name'])
                        # the child's own series: frames in a row around the worksheet numbers, up to a pause
                        # or to someone else's worksheet frame (group photos: only portraits stop the series)
                        own = set(must) | set(sheet)
                        odd = {x: intruder(p['student_id'], x) for x in own}
                        grow = {x for x in own if not odd[x]}
                        if suffix == 'p':
                            near = [f for f in frames if f['path'] in pseries.get(p['student_id'], set())]
                        else:
                            near = self._series(frames, grow, anchors_person, pgap, 8) if grow else []
                            # group photos never take frames from another child's portrait series
                            near = [f for f in near if taken.get(f['path'], p['student_id']) == p['student_id']]
                        have = {f['path'] for f in near}
                        near += [f for f in frames if f['path'] in own and f['path'] not in have]   # suspicious frame alone
                        near.sort(key=lambda f: (f['capture_at'], f['name']))
                        warn = {}
                        for x, who in odd.items():
                            if who:
                                k = must.get(x) or sheet.get(x)
                                warn[k] = f'номер стоит среди кадров: {who} — проверьте номер'
                        if not must and not o:
                            continue  # empty cells are fine: no friends photo, child absent, etc.
                        out.append({'id': p['student_id'] + suffix, 'title': p['name'], 'subtitle': label,
                                    'kind': 'portrait' if suffix == 'p' else 'friends', 'person': p['student_id'],
                                    'files': [f['path'] for f in near], 'mandatory': must, 'limit': None,
                                    'roles': cols, 'overridden': bool(o),
                                    'unsure': {**{k: v for k, v in (a.get('unsure') or {}).items() if k in cols or (k == 'Name' and suffix == 'p')},
                                               **{k: (((a.get('unsure') or {}).get(k) + '; ') if (a.get('unsure') or {}).get(k) else '') + v for k, v in warn.items()}}})
                elif a.get('T'):
                    # teacher given by photo number(s): those frames are mandatory, neighbours shown for choice
                    o = {k: v for k, v in ov[p['student_id']].items() if k == 'T'}
                    must = self._anchors(files, a, ['T'], conflicts, p['name'], o)
                    sheet = self._anchors(files, a, ['T'], [], p['name'])
                    near = self._teacher_series(frames, set(must) | set(sheet), anchors_all)
                    if not must and not o:
                        continue
                    out.append({'id': p['student_id'], 'title': p['name'], 'subtitle': teacher_title(a.get('Role')) or 'Учитель',
                                'kind': 'teacher', 'person': p['student_id'],
                                'files': [f['path'] for f in near], 'mandatory': must, 'limit': 5,
                                'roles': ['T'], 'overridden': bool(o), 'unsure': a.get('unsure') or {}})
                else:
                    s0 = self._resolve(files, a.get('Start'), conflicts, p['name'] + ', начало: ')
                    s1 = self._resolve(files, a.get('End'), conflicts, p['name'] + ', конец: ')
                    lo, hi = sorted([s0['frame'], s1['frame']]) if s0 and s1 else (0, -1)
                    out.append({'id': p['student_id'], 'title': p['name'], 'subtitle': teacher_title(a.get('Role')) or 'Учитель',
                                'kind': 'teacher', 'person': p['student_id'],
                                'files': [f['path'] for f in frames if lo <= f['frame'] <= hi], 'mandatory': {}, 'limit': 5,
                                'roles': [], 'overridden': False})
            # a teacher's set never offers another teacher's worksheet photo
            tmand = {}
            for x in out:
                if x['kind'] == 'teacher':
                    for p in x['mandatory']:
                        tmand[p] = x['id']
            for x in out:
                if x['kind'] == 'teacher':
                    x['files'] = [p for p in x['files'] if tmand.get(p, x['id']) == x['id']]
            out += self._common_sections(files, frames, people, out, conflicts)
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
    @staticmethod
    def _pick_diverse(pool, chosen, quota, hard=2, stop=None, span=None):
        """Farthest-point picking: each next frame is the one least like everything already chosen.
        Near-identical frames (dHash distance <= hard: bursts, bracketing) are never taken.
        With `stop`, picking ends when only frames that close to the chosen ones are left (teachers)."""
        picks, pool = [], list(pool)
        inside = lambda f: bool(span and f['frame'] is not None and span[0] <= f['frame'] <= span[1])
        while len(picks) < quota and pool:
            ref = chosen + picks
            dist = lambda f: min((hamming(f['dhash'], x['dhash']) for x in ref), default=64)
            pool = [f for f in pool if not ref or dist(f) > hard]
            if not pool:
                break
            best = max(pool, key=lambda f: (min(dist(f), 20), inside(f), f['sharpness'] or 0))
            if stop is not None and ref and dist(best) <= stop:
                break
            picks.append(best)
            pool.remove(best)
        return picks

    def _pick_common(self, s, info, dup_d, picks_by):
        """Common photo: the sharpest frame of the longest run of near-identical frames (a group shot is a burst),
        plus its closest repeats as duplicates. Reportage: diverse frames, plus a few near-repeats as duplicates."""
        bad = lambda f: any(x and x != 'dup' for x in (f['flags'] or '').split(','))
        taken = {f['path'] for v in picks_by.values() for f in v}
        cand = [info[p] for p in s['files'] if p in info and not bad(info[p]) and p not in taken and info[p]['dhash']]
        cand.sort(key=lambda f: (f['capture_at'], f['name']))
        if not cand:
            return [], []
        if s['kind'] == 'group':
            must = [info[p] for p in s['mandatory'] if p in info]
            if must:
                main = must[0]
            else:
                runs, cur = [], [cand[0]]
                for a, b in zip(cand, cand[1:]):
                    if hamming(a['dhash'], b['dhash']) <= dup_d + 7:
                        cur.append(b)
                    else:
                        runs.append(cur)
                        cur = [b]
                runs.append(cur)
                run = max(runs, key=len)
                main = max(run, key=lambda f: f['sharpness'] or 0)
            rest = sorted((f for f in cand if f['path'] != main['path']), key=lambda f: hamming(f['dhash'], main['dhash']))
            dups = [f for f in rest if hamming(f['dhash'], main['dhash']) <= dup_d + 12][:s['dup_limit']]
            return ([] if main['path'] in s['mandatory'] else [main]), dups
        main = self._pick_diverse(cand, [], s['main_limit'], hard=2)
        left = [f for f in cand if f not in main]
        dups = []
        for m in main:
            if len(dups) >= s['dup_limit']:
                break
            near = min(left, key=lambda f: hamming(f['dhash'], m['dhash']), default=None)
            if near and hamming(near['dhash'], m['dhash']) <= dup_d + 8:
                dups.append(near)
                left.remove(near)
        return main, dups

    def suggest(self, sid):
        """Assemble kits automatically. A child gets 15–20 frames in total, the 5 worksheet frames included:
        up to 2 extra vignettes (most like the worksheet vignette), extra portraits and extra group photos —
        sharp, without defects, as different from each other as the series allows.
        A teacher gets 3–5 different poses. Sections the photographer already edited are left alone."""
        secs, _ = self.sections(sid)
        st = self.settings()
        kmin, kmax = int(st['kit_min']), int(st['kit_max'])
        target = max(kmin, min(kmax, int(st['target_per_student'])))
        dup_d = int(st['dup_distance'])
        with self.conn() as c:
            info = {f['path']: f for f in self._files(c, sid)}
            c.execute('DELETE FROM ai_suggestions WHERE session_id=?', (sid,))
            approved = {r['section_id'] for r in c.execute('SELECT section_id FROM approvals WHERE session_id=? AND approved=1', (sid,))}
            edited = {r['section_id'] for r in c.execute("SELECT DISTINCT section_id FROM selection_events WHERE session_id=? AND actor='user'", (sid,))}
            picks_by, reason = {}, {}
            seq = sorted(info.values(), key=lambda f: (f['capture_at'], f['name']))
            pos = {f['path']: i for i, f in enumerate(seq)}
            tsec = lambda f: datetime.strptime(f['capture_at'], '%Y-%m-%d %H:%M:%S')
            pgap = int(st['person_gap_sec'])

            def span_of(s):
                ns = [info[p]['frame'] for p in s['mandatory'] if p in info and info[p]['frame'] is not None]
                return (min(ns) - 2, max(ns) + 2) if ns else None

            def safe_side(s, side):
                """True when the series ends with a real pause on this side, so its edge is still this person."""
                ids = sorted(pos[p] for p in s['files'] if p in pos)
                if not ids:
                    return False
                i = ids[0] if side < 0 else ids[-1]
                j = i + side
                if not 0 <= j < len(seq):
                    return True
                return abs((tsec(seq[j]) - tsec(seq[i])).total_seconds()) > pgap

            def pool_of(s):
                # 'Дубль' only means "looks like a neighbour": such frames stay candidates, the picker skips copies
                bad = lambda f: any(x and x != 'dup' for x in (f['flags'] or '').split(','))
                pool = [info[p] for p in s['files'] if p in info and p not in s['mandatory'] and not bad(info[p])]
                sp = span_of(s)
                if sp and s['kind'] in ('portrait', 'friends'):
                    left, right = safe_side(s, -1), safe_side(s, 1)
                    pool = [f for f in pool if f['frame'] is None or sp[0] <= f['frame'] <= sp[1]
                            or (f['frame'] < sp[0] and left) or (f['frame'] > sp[1] and right)]
                return pool
            by_person = defaultdict(list)
            for s in sorted(secs, key=lambda x: {'group': 1, 'reportage': 2}.get(x['kind'], 0)):
                by_person[s['person'] or s['id']].append(s)
            for person, group in by_person.items():
                kinds = {s['kind']: s for s in group}
                if 'portrait' in kinds or 'friends' in kinds:
                    P, F = kinds.get('portrait'), kinds.get('friends')
                    pm = [info[p] for p in (P['mandatory'] if P else {}) if p in info]
                    fm = [info[p] for p in (F['mandatory'] if F else {}) if p in info]
                    m = len(pm) + len(fm)
                    ppool, fpool = (pool_of(P) if P else []), (pool_of(F) if F else [])
                    # extra vignettes: closest to the worksheet vignette, but not a copy of it (3 vignettes at most)
                    vign = []
                    v = next((info[p] for p, k in (P['mandatory'] if P else {}).items() if k == 'V' and p in info), None)
                    if v and v['dhash']:
                        near = sorted((f for f in ppool if f['dhash'] and hamming(f['dhash'], v['dhash']) > 2),
                                      key=lambda f: (hamming(f['dhash'], v['dhash']), -(f['sharpness'] or 0)))
                        for f in near:
                            if len(vign) >= min(2, max(0, target - m)):
                                break
                            if all(hamming(f['dhash'], x['dhash']) > 2 for x in vign):
                                vign.append(f)
                    ppool = [f for f in ppool if f not in vign]
                    budget = max(0, target - m - len(vign))
                    fq = min(len(fpool), round(budget * 0.3)) if F else 0
                    pp = self._pick_diverse(ppool, pm + vign, budget - fq, span=span_of(P)) if P else []
                    fp = self._pick_diverse(fpool, fm, budget - len(pp), span=span_of(F)) if F else []
                    if P and len(pp) + len(fp) < budget:   # friends ran short: more portraits
                        pp += self._pick_diverse([f for f in ppool if f not in pp], pm + vign + pp, budget - len(pp) - len(fp), span=span_of(P))
                    room = max(0, kmax - m)
                    vign, pp, fp = vign[:room], pp[:max(0, room - len(vign))], fp[:max(0, room - len(vign) - len(pp))]
                    if P:
                        picks_by[P['id']] = vign + pp
                        reason.update({(P['id'], f['path']): 'vignette' for f in vign})
                        reason.update({(P['id'], f['path']): 'portrait' for f in pp})
                    if F:
                        picks_by[F['id']] = fp
                        reason.update({(F['id'], f['path']): 'friends' for f in fp})
                for s in group:
                    if s['id'] in picks_by:
                        continue
                    chosen = [info[p] for p in s['mandatory'] if p in info]
                    if s['kind'] in ('group', 'reportage'):
                        main, dups = self._pick_common(s, info, dup_d, picks_by)
                        picks_by[s['id']] = main + dups
                        reason.update({(s['id'], f['path']): 'main' for f in main})
                        reason.update({(s['id'], f['path']): 'dup' for f in dups})
                        continue
                    if s['kind'] == 'teacher':
                        picks_by[s['id']] = self._pick_diverse(pool_of(s), chosen, max(0, 5 - len(chosen)), stop=dup_d + 7)
                    else:
                        picks_by[s['id']] = self._pick_diverse(pool_of(s), chosen, max(1, round(len(s['files']) * 0.35)), stop=dup_d + 3)
            # common photo first, then reportage: they share the free frames
            order = {'group': 0, 'reportage': 1}
            secs_sorted = sorted(secs, key=lambda x: order.get(x['kind'], -1))
            for s in secs:
                picks = picks_by.get(s['id'], [])
                if s['kind'] in ('group', 'reportage') and s['id'] not in edited and s['id'] not in approved:
                    c.execute('DELETE FROM marks WHERE session_id=? AND section_id=?', (sid, s['id']))
                    c.executemany("INSERT INTO marks VALUES(?,?,?,'dup')",
                                  [(sid, s['id'], f['path']) for f in picks if reason.get((s['id'], f['path'])) == 'dup'])
                for f in picks:
                    c.execute('INSERT OR IGNORE INTO ai_suggestions VALUES(?,?,?,?)', (sid, s['id'], f['path'], reason.get((s['id'], f['path']), 'extra')))
                for p in s['mandatory']:   # worksheet frames are always in the kit
                    c.execute('INSERT OR IGNORE INTO selections VALUES(?,?,?)', (sid, s['id'], p))
                if s['id'] in edited or s['id'] in approved:
                    continue   # never overwrite the photographer's own choice
                c.execute('DELETE FROM selections WHERE session_id=? AND section_id=?', (sid, s['id']))
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
            dmarks = defaultdict(list)
            for r in c.execute("SELECT section_id,file_path FROM marks WHERE session_id=? AND mark='dup'", (sid,)):
                dmarks[r['section_id']].append(r['file_path'])
            sugg, reasons = defaultdict(list), defaultdict(dict)
            for r in c.execute('SELECT section_id,file_path,reason FROM ai_suggestions WHERE session_id=?', (sid,)):
                sugg[r['section_id']].append(r['file_path'])
                reasons[r['section_id']][r['file_path']] = r['reason']
        secs, conflicts = self.sections(sid)
        # worksheet frames are always part of the kit; stale picks from an earlier import are dropped
        for x in secs:
            have = [p for p in sel.get(x['id'], []) if p in x['files'] or p in x['mandatory']]
            sel[x['id']] = have + [p for p in x['mandatory'] if p not in have]
        with self.conn() as c:
            people = c.execute("SELECT student_id, name FROM students WHERE session_id=? ORDER BY rowid", (sid,)).fetchall()
        present = {x['person'] for x in secs}
        absent = [r['name'] for r in people if r['student_id'] not in present]
        slim = [{k: f[k] for k in ('path', 'name', 'capture_at', 'kind', 'frame', 'series', 'flags', 'sharpness', 'brightness', 'analyzed')}
                | {'has_preview': bool(f['preview'])} for f in files]
        return {'session': dict(s), 'files': slim, 'sections': secs, 'conflicts': conflicts,
                'selected': sel, 'approved': appr, 'absent': absent, 'reasons': reasons, 'dups': dmarks, 'suggested': sugg, 'pillow': Image is not None,
                'flag_names': FLAG_NAMES}

    def review(self, sid, body):
        secs, _ = self.sections(sid)
        s = next((x for x in secs if x['id'] == body.get('section')), None)
        if not s:
            raise ValueError('Неизвестная секция')
        chosen = body.get('selected')
        if not isinstance(chosen, list) or not set(chosen) <= set(s['files']):
            raise ValueError('Неверный набор кадров')
        dups = body.get('dups')
        if s.get('dup_limit') is not None:
            if dups is None:   # caller did not send duplicates: keep the stored marks
                with self.conn() as c:
                    dups = [r[0] for r in c.execute("SELECT file_path FROM marks WHERE session_id=? AND section_id=? AND mark='dup'", (sid, s['id']))]
            dups = [p for p in dups if p in chosen]
            if len(dups) > s['dup_limit']:
                raise ValueError(f'Дублей не больше {s["dup_limit"]}')
            if len(chosen) - len(dups) > s['main_limit']:
                raise ValueError(f'Основных кадров не больше {s["main_limit"]} — остальные отметьте как дубли (клавиша D)')
        elif s['limit'] and len(chosen) > s['limit']:
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
            if dups is not None:
                c.execute('DELETE FROM marks WHERE session_id=? AND section_id=?', (sid, s['id']))
                c.executemany("INSERT INTO marks VALUES(?,?,?,'dup')", [(sid, s['id'], p) for p in dups])
            c.execute('INSERT OR REPLACE INTO approvals(session_id,section_id,approved,at) VALUES(?,?,?,?)', (sid, s['id'], int(approve), now()))
            self.event(c, sid, 'review', ('approve:' if approve else 'save:') + s['id'])
            if approve:
                total = len(secs)
                done = c.execute('SELECT COUNT(*) FROM approvals WHERE session_id=? AND approved=1', (sid,)).fetchone()[0]
                if done >= total:
                    c.execute("UPDATE sessions SET status='reviewed' WHERE id=? AND status!='exported'", (sid,))
        return {'ok': True}

    def rebuild(self, sid, section_ids):
        """Assemble these kits again from scratch: forget manual picks and confirmations of these sections
        (worksheet roles fixed in the app are kept), then run the automatic assembly."""
        secs, _ = self.sections(sid)
        ids = [x['id'] for x in secs if x['id'] in set(section_ids or [])]
        if not ids:
            raise ValueError('Нечего пересобирать')
        with self.conn() as c:
            for i in ids:
                c.execute("DELETE FROM selection_events WHERE session_id=? AND section_id=? AND actor='user'", (sid, i))
                c.execute('DELETE FROM approvals WHERE session_id=? AND section_id=?', (sid, i))
                c.execute('DELETE FROM selections WHERE session_id=? AND section_id=?', (sid, i))
            self.event(c, sid, 'review', 'rebuild:' + ','.join(ids))
        self.suggest(sid)
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
        # problems do not block the export (the photographer may fix them later); they are reported
        warnings = list(st['conflicts']) + [f"Не подтверждено: {x['title']} — {x['subtitle']}"
                                             for x in st['sections'] if not st['approved'].get(x['id'])]
        files = {f['path']: f for f in st['files']}
        ops, names = [], set()

        def add(p, rel, who='', what='', role=''):
            if str(rel).casefold() in names:
                raise ValueError('Конфликт имён: ' + str(rel))
            names.add(str(rel).casefold())
            ops.append((p, rel, who, what, role))
        if s['mode'] == 'school':
            root = Path(*[safe_name(x or '—') for x in (s['order_no'], s['school'], ru_date(s['shoot_date']), s['class_name'])])
            prefix = {'V': '{n}_', 'P1': 'ЛР_{n}_', 'P2': 'ЛС_{n}_', 'D1': 'Друг1_', 'D2': 'Друг2_', 'T': ''}
            # portraits and friends of one student are merged: a frame inside both windows is copied once
            by_person, subs = defaultdict(set), {}
            mand, roles, teacher_done = {}, {}, set()
            for sec in st['sections']:
                if sec['kind'] in ('group', 'reportage'):
                    cat = CATS[2] if sec['kind'] == 'group' else CATS[3]
                    dset = set(st['dups'].get(sec['id'], []))
                    for p in sorted(st['selected'].get(sec['id'], []), key=lambda x: files[x]['name'] if x in files else x):
                        if p in files:
                            rel = root / cat / ('Дубли' if p in dset else '') / files[p]['name']
                            add(p, rel, sec['title'], sec['title'], 'дубль' if p in dset else 'основной')
                    continue
                key = (sec['person'], sec['kind'] == 'teacher', sec['title'])
                subs[key] = sec['subtitle']
                for p in [x for x in st['selected'].get(sec['id'], []) if x in sec['files'] or x in sec['mandatory']]:
                    by_person[key].add(p)
                for p, k in sec['mandatory'].items():
                    mand[(sec['person'], p)] = prefix[k].format(n=sec['title'])
                    roles[(sec['person'], p)] = ROLE_NAMES.get(k, k)
            for (person, is_teacher, name), paths in by_person.items():
                sub = subs[(person, is_teacher, name)]
                # teacher: the worksheet photo is named «Фамилия Имя Отчество должность»; other poses keep
                # their camera names. Without a worksheet photo, a single chosen frame gets the name.
                main = None
                if is_teacher:
                    marked = sorted(p for p in paths if (person, p) in mand)
                    main = marked[0] if marked else (next(iter(paths)) if len(paths) == 1 else None)
                for p in sorted(paths):
                    nm = files[p]['name']
                    if is_teacher and p != main and p in teacher_done:
                        continue  # the same extra pose chosen for two teachers is copied once
                    if is_teacher:
                        teacher_done.add(p)
                        title = safe_name(' '.join(x for x in (name, teacher_title(sub) if sub != 'Учитель' else '') if x))
                        rel = root / CATS[5] / (title + Path(nm).suffix if p == main else nm)
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
        update = False
        if (out / root).exists() and any((out / root).iterdir()):
            if not (out / root / '_FOBOX_отчёт.csv').exists():
                raise ValueError('Папка заказа уже существует и не пуста: ' + str(out / root))
            update = True  # an earlier FOBOX export: add what is missing, never overwrite or delete
        skip, clash = set(), []
        for op in ops:
            d = out / op[1]
            if d.exists():
                if d.stat().st_size == Path(op[0]).stat().st_size:
                    skip.add(str(op[1]))
                else:
                    clash.append(str(op[1]))
        if clash:
            raise ValueError('В заказе уже есть другой файл с таким именем (перезапись запрещена): ' + clash[0])
        if update:
            wanted = {str(out / op[1]).casefold() for op in ops}
            for f in (out / root).rglob('*'):
                if f.is_file() and not f.name.startswith('_FOBOX_') and str(f).casefold() not in wanted:
                    warnings.append(f'Лишний файл в заказе (снят с выбора, удалите вручную): {f.relative_to(out / root)}')
        need = sum(Path(op[0]).stat().st_size for op in ops if str(op[1]) not in skip)
        free = shutil.disk_usage(out if out.exists() else out.parent if out.parent.exists() else Path.home()).free
        return {'root': str(out / root), 'files': len(ops), 'bytes': need, 'free': free, 'ops': ops, 'out': out,
                'categories': s['mode'] == 'school', 'rootrel': root, 'warnings': warnings,
                'skip': skip, 'update': update, 'new': len(ops) - len(skip)}

    def export(self, sid, output, dry_run=False, job=None):
        job = job if job is not None else {}
        plan = self.plan_export(sid, output)
        if plan['bytes'] > plan['free']:
            raise ValueError('Недостаточно места на диске')
        if dry_run:
            return {k: plan[k] for k in ('root', 'files', 'bytes', 'free', 'warnings', 'update', 'new')}
        with self.conn() as c:
            self.event(c, sid, 'export', 'start')
        root = plan['out'] / plan['rootrel']
        job.update(stage='Копирование', total=len(plan['ops']), done=0, bytes_total=plan['bytes'], bytes_done=0)
        report = []
        for p, rel, who, what, role in plan['ops']:
            d = plan['out'] / rel
            if str(rel) in plan['skip']:      # already copied by an earlier export
                report.append([what, who, role, str(rel.relative_to(plan['rootrel'])), str(p), d.stat().st_size])
                job['done'] += 1
                continue
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
        notes = root / '_FOBOX_замечания.txt'
        if not plan['warnings'] and notes.exists():
            notes.unlink()  # our own notes file: everything is fixed now
        if plan['warnings']:
            with open(notes, 'w', encoding='utf-8-sig') as f:
                f.write('Заказ собран с незакрытыми замечаниями. Исправьте и при необходимости соберите заново:\n\n')
                f.write('\n'.join('- ' + w for w in plan['warnings']) + '\n')
        with self.conn() as c:
            self.event(c, sid, 'export', 'end')
            c.execute("UPDATE sessions SET status='exported' WHERE id=?", (sid,))
        self.set_meta(sid, {'last_export': str(root)})
        job['stage'] = 'Готово'
        return {'root': plan['root'], 'files': plan['files'], 'new': plan['new'], 'update': plan['update'], 'bytes': plan['bytes'], 'report': str(root / '_FOBOX_отчёт.csv'), 'warnings': plan['warnings']}

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
        """Everything the dashboards need, per shoot, so the interface can filter by any period."""
        fmt = '%Y-%m-%d %H:%M:%S'
        with self.conn() as c:
            sessions = [dict(r) for r in c.execute('SELECT * FROM sessions ORDER BY first_capture DESC')]
            per = {s['id']: s for s in sessions}
            for s in sessions:
                s.update(frames=0, raw=0, analyzed=0, flags={}, students=0, kits=0, selected=0, sections=0, approved=0,
                         suggested=0, accepted=0, replaced=0, manual_added=0, untouched=0, durations={}, cover=None, hours=None)
            for r in c.execute("SELECT session_id, COUNT(*) n, SUM(kind='raw') nr, SUM(analyzed) na FROM files GROUP BY session_id"):
                if r['session_id'] in per:
                    per[r['session_id']].update(frames=r['n'], raw=r['nr'] or 0, analyzed=r['na'] or 0)
            for r in c.execute('SELECT session_id, flags FROM files WHERE flags IS NOT NULL'):
                fl = per.get(r['session_id'], {}).get('flags')
                if fl is not None:
                    for f in r['flags'].split(','):
                        fl[f] = fl.get(f, 0) + 1
            days, day_hours = defaultdict(int), defaultdict(float)
            for r in c.execute('SELECT substr(capture_at,1,10) d, COUNT(*) n FROM files GROUP BY d'):
                days[r['d']] += r['n']
            for r in c.execute("SELECT session_id, COUNT(*) n FROM students WHERE role='student' GROUP BY session_id"):
                if r['session_id'] in per:
                    per[r['session_id']]['students'] = r['n']
            for r in c.execute('SELECT session_id, COUNT(DISTINCT file_path) n FROM selections GROUP BY session_id'):
                if r['session_id'] in per:
                    per[r['session_id']]['selected'] = r['n']
            for r in c.execute('SELECT session_id, COUNT(*) n, SUM(approved) a FROM approvals GROUP BY session_id'):
                if r['session_id'] in per:
                    per[r['session_id']]['approved'] = r['a'] or 0
            for r in c.execute('SELECT session_id, COUNT(*) n FROM ai_suggestions GROUP BY session_id'):
                if r['session_id'] in per:
                    per[r['session_id']]['suggested'] = r['n']
            for r in c.execute('''SELECT a.session_id, COUNT(*) n FROM ai_suggestions a JOIN selections s USING(session_id,section_id,file_path)
                                JOIN approvals p ON p.session_id=a.session_id AND p.section_id=a.section_id AND p.approved=1
                                GROUP BY a.session_id'''):
                if r['session_id'] in per:
                    per[r['session_id']]['accepted'] = r['n']
            for r in c.execute('''SELECT a.session_id, COUNT(*) n FROM ai_suggestions a JOIN approvals p ON p.session_id=a.session_id
                                AND p.section_id=a.section_id AND p.approved=1
                                WHERE NOT EXISTS(SELECT 1 FROM selections s WHERE s.session_id=a.session_id
                                AND s.section_id=a.section_id AND s.file_path=a.file_path) GROUP BY a.session_id'''):
                if r['session_id'] in per:
                    per[r['session_id']]['replaced'] = r['n']
            for r in c.execute('''SELECT s.session_id, COUNT(*) n FROM selections s JOIN approvals p ON p.session_id=s.session_id
                                AND p.section_id=s.section_id AND p.approved=1
                                WHERE s.file_path NOT IN (SELECT file_path FROM ai_suggestions a WHERE a.session_id=s.session_id AND a.section_id=s.section_id)
                                AND NOT EXISTS(SELECT 1 FROM selection_events e WHERE e.session_id=s.session_id AND e.section_id=s.section_id
                                AND e.file_path=s.file_path AND e.actor='worksheet') GROUP BY s.session_id'''):
                if r['session_id'] in per:
                    per[r['session_id']]['manual_added'] = r['n']
            for r in c.execute('''SELECT p.session_id, COUNT(*) n FROM approvals p WHERE p.approved=1 AND NOT EXISTS(
                                SELECT 1 FROM selection_events e WHERE e.session_id=p.session_id AND e.section_id=p.section_id AND e.actor='user')
                                GROUP BY p.session_id'''):
                if r['session_id'] in per:
                    per[r['session_id']]['untouched'] = r['n']
            wf = [dict(r) for r in c.execute('SELECT session_id,stage,event,at FROM workflow_events ORDER BY id')]
            for s in sessions:
                cov = c.execute('''SELECT e.file_path FROM selection_events e JOIN files f ON f.session_id=e.session_id AND f.path=e.file_path
                                    WHERE e.session_id=? AND e.actor='worksheet' AND f.preview IS NOT NULL LIMIT 1''', (s['id'],)).fetchone() or \
                      c.execute('SELECT path FROM files WHERE session_id=? AND preview IS NOT NULL AND flags IS NULL ORDER BY capture_at LIMIT 1 OFFSET 3', (s['id'],)).fetchone() or \
                      c.execute('SELECT path FROM files WHERE session_id=? AND preview IS NOT NULL LIMIT 1', (s['id'],)).fetchone()
                s['cover'] = cov[0] if cov else None
                secs = self.sections(s['id'])[0] if s['frames'] else []
                s['sections'] = len(secs)
                ok = {r[0] for r in c.execute('SELECT section_id FROM approvals WHERE session_id=? AND approved=1', (s['id'],))}
                people = defaultdict(list)
                for x in secs:
                    if x['person'] and x['kind'] != 'teacher':
                        people[x['person']].append(x['id'] in ok)
                s['kits'] = sum(1 for v in people.values() if all(v))   # a child is ready when all its sections are confirmed
        for s in sessions:
            if s['first_capture'] and s['last_capture']:
                s['hours'] = round((datetime.strptime(s['last_capture'], fmt) - datetime.strptime(s['first_capture'], fmt)).total_seconds() / 3600, 2)
                if s['shoot_date']:
                    day_hours[s['shoot_date'][:10]] += s['hours']
        # workflow: start/end pairs per stage; review = time between consecutive confirmations (pauses > 30 min ignored)
        opened, appr = {}, defaultdict(list)
        for e in wf:
            s = per.get(e['session_id'])
            if not s:
                continue
            k = (e['session_id'], e['stage'])
            t = datetime.strptime(e['at'], fmt)
            if e['event'] == 'start':
                opened[k] = t
            elif e['event'] == 'end' and k in opened:
                s['durations'][e['stage']] = s['durations'].get(e['stage'], 0) + (t - opened.pop(k)).total_seconds()
            elif e['stage'] == 'review' and e['event'].startswith('approve:'):
                appr[e['session_id']].append(t)
        for sid, ts in appr.items():
            ts.sort()
            gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:]) if (b - a).total_seconds() < 1800]
            per[sid]['durations']['review'] = sum(gaps)
            per[sid]['durations']['review_per_section'] = round(sum(gaps) / len(gaps), 1) if gaps else None
        months = defaultdict(lambda: {'frames': 0, 'sessions': 0})
        for d, n in days.items():
            months[d[:7]]['frames'] += n
        for s in sessions:
            if s['shoot_date']:
                months[s['shoot_date'][:7]]['sessions'] += 1
        flags = Counter()
        for s in sessions:
            flags.update(s['flags'])
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
        big = [s for s in sessions if s['students']]
        if big:
            x = max(big, key=lambda s: s['students'])
            records['biggest_class'] = {'value': x['students'], 'title': x['title'], 'date': x['shoot_date']}
        tot = lambda k: sum(s[k] for s in sessions)
        durs = defaultdict(list)
        for s in sessions:
            for k, v in s['durations'].items():
                if v is not None:
                    durs[k].append(v)
        avg = lambda v: round(sum(v) / len(v), 1) if v else None
        return {
            'sessions': sessions, 'files': tot('frames'), 'raw': tot('raw'), 'analyzed': tot('analyzed'), 'flags': dict(flags),
            'hours': round(sum(s['hours'] for s in with_hours), 1) if with_hours else None, 'kits': tot('kits'),
            'days': dict(days), 'day_hours': {k: round(v, 2) for k, v in day_hours.items()},
            'months': {k: months[k] for k in sorted(months)},
            'selection': {'suggested': tot('suggested'), 'accepted': tot('accepted'), 'replaced': tot('replaced'),
                          'manual_added': tot('manual_added'), 'sections_untouched': tot('untouched'), 'sections_approved': tot('approved')},
            'workflow': {'import_sec': avg(durs['import']), 'analysis_sec': avg(durs['analysis']),
                         'export_sec': avg(durs['export']), 'review_per_section_sec': avg(durs['review_per_section'])},
            'records': records, 'pillow': Image is not None,
        }
