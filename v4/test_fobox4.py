"""Tests for FOBOX 4.0 engine. Run:  py -3 -m unittest test_fobox4 -v
Uses only synthetic files created in a temporary folder."""
import hashlib
import io
import struct
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from engine import Engine, read_meta

try:
    from PIL import Image
except ImportError:
    Image = None


def jpeg(color, when, blank=False):
    im = Image.new('RGB', (300, 200), color)
    if not blank:
        from PIL import ImageDraw
        d = ImageDraw.Draw(im)
        d.ellipse([100, 40, 180, 120], fill=(230, 200, 180))
        d.rectangle([90, 120, 190, 200], fill=(30, 40, 60))
        d.line([0, 0, 300, 200], fill=(255, 255, 255), width=3)
    ex = Image.Exif()
    ex.get_ifd(0x8769)[0x9003] = when.strftime('%Y:%m:%d %H:%M:%S')
    buf = io.BytesIO()
    im.save(buf, 'JPEG', exif=ex)
    return buf.getvalue()


def fake_tiff_raw(jpg):
    """Minimal little-endian TIFF ("RAW") with an embedded JPEG preview, no sidecar."""
    entries = [(0x0201, 4, 1, 0), (0x0202, 4, 1, len(jpg)), (0x0132, 2, 20, 0)]
    ifd_off = 8
    ifd_size = 2 + len(entries) * 12 + 4
    date_off = ifd_off + ifd_size
    jpg_off = date_off + 20
    entries[0] = (0x0201, 4, 1, jpg_off)
    entries[2] = (0x0132, 2, 20, date_off)
    out = b'II*\0' + struct.pack('<I', ifd_off) + struct.pack('<H', len(entries))
    for tag, typ, cnt, val in sorted(entries):
        out += struct.pack('<HHII', tag, typ, cnt, val)
    out += struct.pack('<I', 0) + b'2026:05:01 12:00:00\0' + jpg
    return out


def digest(folder):
    h = hashlib.sha1()
    for p in sorted(Path(folder).rglob('*')):
        if p.is_file():
            h.update(p.name.encode() + p.read_bytes() + str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


@unittest.skipIf(Image is None, 'Pillow required for synthetic images')
class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / 'card'
        self.src.mkdir()
        t = datetime(2026, 10, 9, 9, 0, 0)
        for n in range(100, 140):
            t += timedelta(seconds=5 if n != 120 else 900)
            colour = (60 + n % 7 * 20, 90, 140)
            (self.src / f'ABC0{n}.JPG').write_bytes(jpeg((252, 252, 252) if n == 105 else colour, t, blank=(n == 105)))
            (self.src / f'ABC0{n}.ARW').write_bytes(b'RAW' * 50 + bytes([n % 256]))
        (self.tmp / 'students.csv').write_text('Name,V,P1,P2,D1,D2\nИванов Иван Петрович,110,111,112,125,126\n', encoding='utf-8')
        self.e = Engine(self.tmp / 'db' / 'f.sqlite3')

    def school(self):
        return self.e.import_folder(self.src, {'students': str(self.tmp / 'students.csv'), 'order': '1', 'school': 'Школа',
                                               'class_name': '1А', 'date': '09.10.2026'})

    def approve_all(self, sid):
        st = self.e.session(sid)
        for s in st['sections']:
            self.e.review(sid, {'section': s['id'], 'selected': st['selected'][s['id']], 'approved': True})

    def test_import_is_idempotent_and_series_split(self):
        sid = self.e.import_folder(self.src)
        self.assertEqual(sid, self.e.import_folder(self.src))
        st = self.e.session(sid)
        self.assertEqual(len(st['files']), 40)                    # RAW+JPEG pairs count once
        self.assertEqual(len({f['series'] for f in st['files']}), 2)  # 15-minute pause splits series
        self.assertEqual(self.e.stats()['files'], 40)

    def test_blank_frame_flagged_but_kept(self):
        sid = self.e.import_folder(self.src)
        f = next(f for f in self.e.session(sid)['files'] if f['name'] == 'ABC0105.ARW')
        self.assertIn('white', f['flags'] or '')

    def test_sources_never_modified_by_full_cycle(self):
        before = digest(self.src)
        sid = self.school()
        self.approve_all(sid)
        self.e.export(sid, self.tmp / 'out')
        self.assertEqual(before, digest(self.src))

    def test_school_export_names_and_no_overwrite(self):
        sid = self.school()
        with self.assertRaises(ValueError):           # not approved yet
            self.e.export(sid, self.tmp / 'out')
        self.approve_all(sid)
        r = self.e.export(sid, self.tmp / 'out')
        kid = Path(r['root']) / '05_УЧЕНИКИ' / 'Иванов Иван'
        names = {p.name for p in kid.iterdir()}
        for n in ('Иванов Иван_ABC0110.ARW', 'ЛР_Иванов Иван_ABC0111.ARW', 'ЛС_Иванов Иван_ABC0112.ARW',
                  'Друг1_ABC0125.ARW', 'Друг2_ABC0126.ARW'):
            self.assertIn(n, names)
        self.assertTrue((Path(r['root']) / '07_ФОН').is_dir())
        with self.assertRaises(ValueError):           # second export into non-empty order folder
            self.e.export(sid, self.tmp / 'out')

    def test_frame_in_portraits_and_friends_copied_once(self):
        sid = self.school()
        st = self.e.session(sid)
        p, f = st['sections'][0], st['sections'][1]
        shared = next(x for x in p['files'] if x in f['files'] and x not in p['mandatory'] and x not in f['mandatory'])
        for s in (p, f):
            self.e.review(sid, {'section': s['id'], 'selected': list(s['mandatory']) + [shared], 'approved': True})
        r = self.e.export(sid, self.tmp / 'out')
        self.assertEqual(r['files'], 6)

    def test_excel_worksheet_with_russian_headers(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl not installed (only needed to create the test file)')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Заказ 7470, 9Л'])                      # title row above the header
        ws.append(['Фамилия Имя', 'Виньетка', 'Личный разворот', 'Личная страница', 'Друг 1', 'Друг 2'])
        ws.append(['Иванов Иван', 110, 111, 112, 125, 126])  # numbers as Excel numbers
        ws.append([None, None, None, None, None, None])     # empty row is skipped
        xlsx = self.tmp / 'ведомость.xlsx'
        wb.save(xlsx)
        sid = self.e.import_folder(self.src, {'students': str(xlsx), 'order': '1', 'school': 'Школа', 'class_name': '1А'})
        st = self.e.session(sid)
        self.assertEqual(st['conflicts'], [])
        p = st['sections'][0]
        self.assertEqual(p['title'], 'Иванов Иван')
        self.assertEqual(sorted(p['mandatory'].values()), ['P1', 'P2', 'V'])
        self.assertEqual(sorted(st['sections'][1]['mandatory'].values()), ['D1', 'D2'])
        self.approve_all(sid)
        r = self.e.export(sid, self.tmp / 'out')
        names = {x.name for x in (Path(r['root']) / '05_УЧЕНИКИ' / 'Иванов Иван').iterdir()}
        self.assertTrue({'Иванов Иван_ABC0110.ARW', 'ЛР_Иванов Иван_ABC0111.ARW', 'ЛС_Иванов Иван_ABC0112.ARW',
                         'Друг1_ABC0125.ARW', 'Друг2_ABC0126.ARW'} <= names)

    def test_combined_worksheet_pasted_in_one_excel_column(self):
        """The real-life layout: CSV text in column A, type column, teachers in the same file,
        empty cells, two numbers in one cell, a frame shared by two children."""
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl not installed')
        lines = ['Тип;Фамилия Имя;Виньетка;ЛР;ЛС;Друг1;Друг2;Фото учителя;Прочее',
                 'ученики;Иванов Иван;110;111;112;125;126;;',
                 'ученики;Петрова Анна;;114;115;125;127 128;;',
                 'учителя;Сидорова Мария;;;;;;135;']
        wb = openpyxl.Workbook()
        for l in lines:
            wb.active.append([l])
        xlsx = self.tmp / 'список.xlsx'
        wb.save(xlsx)
        sid = self.e.import_folder(self.src, {'students': str(xlsx), 'order': '1', 'school': 'Ш', 'class_name': '1А'})
        st = self.e.session(sid)
        self.assertEqual(st['conflicts'], [])
        secs = {s['id']: s for s in st['sections']}
        self.assertEqual(len(secs['s1f']['mandatory']), 3)       # 125 + "127 128"
        self.assertEqual(len(secs['s1p']['mandatory']), 2)       # empty vignette is allowed
        self.assertEqual(secs['t0']['title'], 'Сидорова Мария')
        self.assertEqual(len(secs['t0']['mandatory']), 1)
        self.approve_all(sid)
        root = Path(self.e.export(sid, self.tmp / 'out')['root'])
        anna = {x.name for x in (root / '05_УЧЕНИКИ' / 'Петрова Анна').iterdir()}
        self.assertTrue({'ЛР_Петрова Анна_ABC0114.ARW', 'Друг1_ABC0125.ARW', 'Друг2_ABC0127.ARW', 'Друг2_ABC0128.ARW'} <= anna)
        self.assertIn('Друг1_ABC0125.ARW', {x.name for x in (root / '05_УЧЕНИКИ' / 'Иванов Иван').iterdir()})
        self.assertTrue(any((root / '06_УЧИТЕЛЯ').iterdir()))

    def test_missing_number_is_reported_with_child_name(self):
        f = self.tmp / 'v.csv'
        f.write_text('Иванов Иван;110;111;112;125;999\n', encoding='utf-8')   # no header at all
        sid = self.e.import_folder(self.src, {'students': str(f)})
        c = self.e.session(sid)['conflicts']
        self.assertEqual(c, ['Иванов Иван, Друг 2: 999 — нет в папке'])

    def test_empty_cells_and_absent_child_do_not_block_export(self):
        f = self.tmp / 'v.csv'
        f.write_text('ФИО;В;ЛР;ЛС;Друг1;Друг2\nИванов Иван;110;111;112;;\nОтсутствующий Пётр;;;;;\n', encoding='utf-8')
        sid = self.e.import_folder(self.src, {'students': str(f)})
        st = self.e.session(sid)
        self.assertEqual([s['id'] for s in st['sections']], ['s0p'])   # no friends section, nothing for the absent child
        self.assertEqual(st['absent'], ['Отсутствующий Пётр'])
        self.assertEqual(st['conflicts'], [])
        self.approve_all(sid)
        self.assertEqual(self.e.export(sid, self.tmp / 'out', dry_run=True)['files'] >= 3, True)

    def test_wrong_worksheet_frame_can_be_replaced_in_app(self):
        sid = self.school()
        st = self.e.session(sid)
        p = st['sections'][0]
        wrong = next(x for x, k in p['mandatory'].items() if k == 'P1')
        right = next(x for x in p['files'] if x not in p['mandatory'])
        self.e.set_role(sid, {'section': p['id'], 'path': wrong, 'role': None})       # "not this person"
        st = self.e.session(sid)
        p = st['sections'][0]
        self.assertNotIn(wrong, p['mandatory'])
        self.assertNotIn(wrong, st['selected'][p['id']])
        self.e.set_role(sid, {'section': p['id'], 'path': right, 'role': 'P1'})
        self.approve_all(sid)
        r = self.e.export(sid, self.tmp / 'out')
        names = {x.name for x in (Path(r['root']) / '05_УЧЕНИКИ' / 'Иванов Иван').iterdir()}
        self.assertIn('ЛР_Иванов Иван_' + Path(right).name, names)
        self.assertNotIn('ЛР_Иванов Иван_ABC0111.ARW', names)
        self.e.set_role(sid, {'section': p['id'], 'reset': True})                     # back to the worksheet
        self.assertEqual(self.e.session(sid)['sections'][0]['mandatory'].get(wrong), 'P1')

    def test_raw_and_jpeg_in_different_folders_are_paired(self):
        d = self.tmp / 'split'
        (d / 'RAW').mkdir(parents=True)
        (d / 'JPG').mkdir()
        for n in (1, 2, 3):
            (d / 'RAW' / f'ABC0000{n}.ARW').write_bytes(b'RAW' + bytes([n]))
            (d / 'JPG' / f'ABC0000{n}.JPG').write_bytes(jpeg((90, 90, 90), datetime(2026, 1, 1, 10, 0, n)))
        sid = self.e.import_folder(d)
        files = self.e.session(sid)['files']
        self.assertEqual(len(files), 3)
        self.assertTrue(all(f['kind'] == 'raw' and f['has_preview'] for f in files))

    def test_standalone_jpeg_hidden_when_card_has_raw(self):
        (self.src / 'Полянская Зина.JPG').write_bytes(jpeg((90, 90, 90), datetime(2026, 10, 9, 9, 0, 30)))
        sid = self.e.import_folder(self.src)
        st = self.e.session(sid)
        offered = {p for s in st['sections'] for p in s['files']}
        self.assertFalse(any(p.endswith('.JPG') for p in offered))

    def test_vertical_raw_preview_is_rotated(self):
        d = self.tmp / 'vert'
        d.mkdir()
        raw = bytearray(fake_tiff_raw(jpeg((10, 120, 200), datetime(2026, 5, 1, 12))))
        # add Orientation=6 (camera held vertically) to IFD0: patch entry count and append an entry
        n = struct.unpack_from('<H', raw, 8)[0]
        entries = [raw[10 + i * 12:22 + i * 12] for i in range(n)] + [struct.pack('<HHII', 0x0112, 3, 1, 6)]
        shift = 12
        fixed = []
        for e in entries:
            tag, typ, cnt, val = struct.unpack('<HHII', e)
            if tag in (0x0201, 0x0132):
                val += shift
            fixed.append(struct.pack('<HHII', tag, typ, cnt, val))
        body = raw[10 + n * 12:]
        raw = raw[:8] + struct.pack('<H', n + 1) + b''.join(sorted(fixed)) + body
        (d / 'DSC_0002.ARW').write_bytes(bytes(raw))
        sid = self.e.import_folder(d)
        data = self.e.preview_bytes(sid, str(d / 'DSC_0002.ARW'))
        w, h = Image.open(io.BytesIO(data)).size
        self.assertGreater(h, w)                 # 300x200 landscape preview shown upright as portrait

    def test_csv_semicolon_cp1251(self):
        f = self.tmp / 'v.csv'
        f.write_bytes('ФИО;В;П1;П2;Д1;Д2\r\nИванов Иван;110;111;112;125;126\r\n'.encode('cp1251'))
        sid = self.e.import_folder(self.src, {'students': str(f)})
        self.assertEqual(self.e.session(sid)['conflicts'], [])

    def test_mandatory_frames_cannot_be_removed(self):
        sid = self.school()
        st = self.e.session(sid)
        s = st['sections'][0]
        with self.assertRaises(ValueError):
            self.e.review(sid, {'section': s['id'], 'selected': [], 'approved': True})

    def test_export_outside_source_only(self):
        sid = self.e.import_folder(self.src)
        self.approve_all(sid)
        with self.assertRaises(ValueError):
            self.e.export(sid, self.src / 'export')

    def test_selection_stats(self):
        sid = self.school()
        st = self.e.session(sid)
        s = st['sections'][0]
        extra = [p for p in st['selected'][s['id']] if p not in s['mandatory']]
        keep = [p for p in st['selected'][s['id']] if p not in extra[:1]]
        self.e.review(sid, {'section': s['id'], 'selected': keep, 'approved': True})
        sel = self.e.stats()['selection']
        self.assertEqual(sel['replaced'], 1 if extra else 0)
        self.assertEqual(sel['sections_approved'], 1)

    def test_embedded_raw_preview_without_sidecar(self):
        d = self.tmp / 'rawonly'
        d.mkdir()
        (d / 'DSC_0001.NEF').write_bytes(fake_tiff_raw(jpeg((10, 120, 200), datetime(2026, 5, 1, 12))))
        meta = read_meta(d / 'DSC_0001.NEF')
        self.assertEqual(meta['capture_at'], '2026-05-01 12:00:00')
        sid = self.e.import_folder(d)
        f = self.e.session(sid)['files'][0]
        self.assertTrue(f['has_preview'])
        self.assertTrue(self.e.preview_bytes(sid, str(d / 'DSC_0001.NEF')).startswith(b'\xff\xd8'))


if __name__ == '__main__':
    unittest.main()
