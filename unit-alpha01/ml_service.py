"""Multi-Layer service inside LUMIO. SQLite is working state, not a second ULS registry."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from ml_core import build, instant, iso, stable
from ml_contracts import validate, validate_result
from ml_metadata import Cancelled, command, extract, inventory, tags_for, tool, probe
from ml_outputs import atomic_json, outputs


class MultiLayer:
    def __init__(self, lab):
        self.lab, self.e, self.unit = lab, lab.e, lab.unit
        with self.e.conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS ml_projects(session_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ml_assets(session_id TEXT,asset_id TEXT,data TEXT NOT NULL,PRIMARY KEY(session_id,asset_id));
            CREATE TABLE IF NOT EXISTS ml_analyses(session_id TEXT,revision INTEGER,data TEXT NOT NULL,folder TEXT,
               PRIMARY KEY(session_id,revision));
            ''')
            cols = {r['name'] for r in c.execute('PRAGMA table_info(lab_jobs)')}
            if 'cancel_requested' not in cols:
                c.execute('ALTER TABLE lab_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0')

    def folder(self, sid):
        self.lab.validate(sid)
        return self.lab.root / sid / 'MULTILAYER'

    def create(self, title):
        sid=uuid.uuid4().hex[:12]
        originals=self.lab.root/sid/'originals';originals.mkdir(parents=True)
        with self.e.conn() as c:
            c.execute('INSERT INTO sessions(id,title,company,source,mode,status) VALUES(?,?,?,?,?,?)',
                      (sid,str(title).strip()[:180] or 'UNIT · новое событие','unitlab',str(originals),'general','imported'))
            c.execute('INSERT INTO lab_projects VALUES(?,?)',(sid,str(title).strip()[:180] or 'UNIT · новое событие'))
        self.state(sid)
        return {'session':sid}

    def state(self, sid):
        session, _ = self.lab.validate(sid)
        with self.e.conn() as c:
            row = c.execute('SELECT * FROM ml_projects WHERE session_id=?', (sid,)).fetchone()
        if row:
            return json.loads(row['data']), row['revision']
        config = {'schema_version': 'unit.multilayer.event/1', 'record_id': 'local:' + str(uuid.uuid4()),
                  'event_id': None, 'shoot_id': None, 'uls_record_id': None, 'title': session['title'],
                  'timezone': 'Europe/Moscow', 'allow_naive': True, 'event_start': None, 'event_end': None,
                  'interval_minutes': 'AUTO', 'activity_scope': 'master_photo_captures', 'master_mode': 'all',
                  'devices': [{'device_id': 'SONY-01', 'role': 'MASTER_PHOTO', 'offset_seconds': 0, 'priority': 0, 'timezone': '', 'quicktime_utc': False}],
                  'sources': [{'device_id': 'SONY-01', 'root': session['source']}], 'tools': {}}
        state = {'config': config, 'review': {'selection': {}, 'time_overrides': {}, 'manual_order': [], 'markers': []}, 'prepared':{},
                 'history': [], 'derivatives': [], 'latest_analysis': None}
        with self.e.conn() as c:
            c.execute('INSERT OR IGNORE INTO ml_projects VALUES(?,0,?)', (sid, json.dumps(state, ensure_ascii=False)))
            row = c.execute('SELECT * FROM ml_projects WHERE session_id=?', (sid,)).fetchone()
        return json.loads(row['data']), row['revision']

    def latest(self, sid):
        state, _ = self.state(sid)
        if not state['latest_analysis']:
            return None
        with self.e.conn() as c:
            row = c.execute('SELECT data FROM ml_analyses WHERE session_id=? AND revision=?', (sid, state['latest_analysis'])).fetchone()
        return json.loads(row['data']) if row else None

    def project(self, sid):
        state, revision = self.state(sid)
        result = self.latest(sid)
        stale=bool(result and result['input_config_fingerprint']!=stable({'assets':[(a['asset_id'],a.get('raw_timestamp')) for a in self.raw_assets(sid)],'config':state['config'],'review':state['review'],'prepared':state.get('prepared',{})}))
        return {'revision': revision, 'state': state, 'result': self.public(result) if result else None,'needs_rebuild':stale,
                'tools': self.tool_status(state['config']), 'bridge_verified': False}

    @staticmethod
    def tool_status(config):
        return {name: {'ready': bool(tool(name, config)), 'path': tool(name, config)} for name in ('exiftool', 'ffprobe', 'ffmpeg')}

    @staticmethod
    def public(result):
        def strip(a):
            return {k: v for k, v in a.items() if k not in ('metadata', 'probe', 'serial', 'gps', 'locations')}
        return {**result, 'assets': [strip(a) for a in result['assets']],
                'captures': [strip(a) for a in result['captures']],
                'timeline': {**result['timeline'], 'items': [strip(a) for a in result['timeline']['items']]}}

    def busy(self, sid):
        with self.e.conn() as c:
            return bool(c.execute("SELECT 1 FROM lab_jobs WHERE session_id=? AND status IN ('queued','running')", (sid,)).fetchone())

    def save(self, sid, incoming, revision, config_change=False):
        if self.busy(sid):
            raise ValueError('Дождитесь задачи или отмените её перед изменением параметров')
        old, _ = self.state(sid)
        new = deepcopy(old)
        if config_change:
            config = {**old['config'], **incoming}
            # IDs imported from Generator are never inferred from matching digits.
            for key in ('record_id', 'uls_record_id', 'shoot_id'):
                config[key] = old['config'].get(key)
            if config.get('event_id') == '':config['event_id'] = None
            if config.get('event_start') or config.get('event_end'):
                config.setdefault('bounds_reason','Задано вручную в UNIT')
            validate('event', config)
            if config.get('event_id'):
                with self.e.conn() as c:
                    others=[json.loads(r['data'])['config'] for r in c.execute('SELECT data FROM ml_projects WHERE session_id!=?',(sid,))]
                for other in others:
                    if other.get('event_id')==config['event_id'] and other.get('record_id')!=config['record_id']:
                        year=str(config.get('date') or '')[:4];other_year=str(other.get('date') or '')[:4]
                        if not year or not other_year or year==other_year:
                            raise ValueError('Такой EVENT ID уже есть в другом рабочем проекте; свяжите существующую запись или оставьте номер пустым')
            from zoneinfo import ZoneInfo
            ZoneInfo(config['timezone'])
            if config.get('interval_minutes') not in ('AUTO', 5, 10, 30):raise ValueError('Интервал: AUTO / 5 / 10 / 30')
            if config.get('master_mode') not in ('all', 'selected'):raise ValueError('Неверный режим фото-мастера')
            if config.get('activity_scope') not in ('master_photo_captures', 'all_photo_devices'):raise ValueError('Неверный activity scope')
            devices = {d['device_id'] for d in config['devices']}
            if len(devices) != len(config['devices']):raise ValueError('Повтор устройства')
            if sum(d['role'] == 'MASTER_PHOTO' for d in config['devices']) > 1:raise ValueError('Фото-мастер должен быть один')
            if len(config.get('sources', [])) > 50:raise ValueError('Слишком много папок')
            for source in config.get('sources', []):
                if source['device_id'] not in devices or not str(source.get('root', '')).strip():raise ValueError('Назначьте папке устройство')
                root = Path(source['root']).expanduser().resolve()
                managed = Path(self.unit.rows(sid)[0]['source']).resolve()
                if self.lab.root == root or root in self.lab.root.parents or (self.lab.root in root.parents and root != managed):
                    raise ValueError('Нельзя сканировать WORK/CACHE/EXPORTS или рабочий каталог приложения')
                source['root'] = str(root)
            for key in ('event_start', 'event_end'):
                if config.get(key):instant(config[key], config['timezone'])
            new['config'] = config
        else:
            review = {**old['review'], **incoming}
            with self.e.conn() as c:
                ids = {r[0] for r in c.execute('SELECT asset_id FROM ml_assets WHERE session_id=?', (sid,))}
            if any(k not in ids or v not in ('undecided', 'keep', 'reject') for k, v in review['selection'].items()):raise ValueError('Неверное решение отбора')
            if len(review.get('manual_order', [])) != len(set(review.get('manual_order', []))):raise ValueError('Повтор в ручном порядке')
            validate('markers', {'markers': review.get('markers', [])})
            if any(m['asset_id'] not in ids for m in review.get('markers', [])):raise ValueError('MARK не связан с материалом')
            for aid, value in review.get('time_overrides', {}).items():
                if aid not in ids:raise ValueError('Неизвестный asset')
                instant(value, new['config']['timezone'])
            new['review'] = review
        new['history'] = (old.get('history', []) + [{'config': old['config'], 'review': old['review']}])[-25:]
        self.commit(sid, new, revision)
        return {'revision': revision + 1, 'needs_rebuild': True}

    def commit(self, sid, state, revision):
        with self.e.lock, self.e.conn() as c:
            actual = c.execute('SELECT revision FROM ml_projects WHERE session_id=?', (sid,)).fetchone()
            if not actual or actual[0] != revision:raise ValueError('Проект изменился. Обновите страницу.')
            c.execute('UPDATE ml_projects SET revision=?,data=? WHERE session_id=?', (revision + 1, json.dumps(state, ensure_ascii=False, allow_nan=False), sid))
        self.e.offline.dirty.add(sid)

    def cancel(self, jid):
        with self.e.conn() as c:
            row = c.execute('SELECT kind FROM lab_jobs WHERE id=?', (jid,)).fetchone()
            if not row or not row['kind'].startswith('ml-'):raise ValueError('Эта задача не поддерживает отмену')
            c.execute('UPDATE lab_jobs SET cancel_requested=1 WHERE id=?', (jid,))
        return {'ok': True}

    def check_cancel(self, jid):
        with self.e.conn() as c:
            row = c.execute('SELECT cancel_requested FROM lab_jobs WHERE id=?', (jid,)).fetchone()
        if row and row[0]:raise Cancelled('Задача отменена; исходники и предыдущая версия сохранены')

    def enqueue(self, sid, operation, payload):
        self.lab.validate(sid)
        self.state(sid)
        if operation not in ('analyze', 'sync', 'working', 'final'):raise ValueError('Неизвестная операция')
        return self.lab.enqueue(sid, 'ml-' + operation, payload)

    def run(self, operation, sid, body, jid):
        cancel = lambda: self.check_cancel(jid)
        if operation == 'analyze':return self.analyze(sid, jid, cancel)
        if operation == 'sync':return self.rebuild(sid, jid, cancel)
        if operation in ('working', 'final'):return self.export(sid, operation, body, jid, cancel)
        raise ValueError('Неизвестная задача Multi-Layer')

    def analyze(self, sid, jid, cancel):
        from lab_workspace import sha256
        state, revision = self.state(sid)
        config = state['config']
        folder = self.folder(sid);folder.mkdir(parents=True, exist_ok=True)
        scratch = folder / 'CACHE';scratch.mkdir(exist_ok=True)
        with self.e.conn() as c:
            previous = {r['asset_id']: json.loads(r['data']) for r in c.execute('SELECT * FROM ml_assets WHERE session_id=?', (sid,))}
            origins = {r['sha256']: r['source'] for r in c.execute('SELECT * FROM lab_sources WHERE session_id=?', (sid,))}
        locations, warnings = [], []
        devices = {d['device_id']: d for d in config['devices']}
        seen_paths = set()
        for source in config['sources']:
            cancel()
            try:
                for path in inventory(source['root']):
                    key = str(path.resolve()).casefold()
                    if key not in seen_paths:
                        locations.append((source, path));seen_paths.add(key)
            except (OSError, ValueError) as exc:
                warnings.append({'message': str(exc)})
        self.lab.update(jid, stage='Чтение metadata', total=len(locations), done=0)
        try:
            tags, notes = tags_for([p for _, p in locations], config, scratch, cancel)
            warnings.extend({'message': n} for n in notes)
        except (OSError, ValueError) as exc:
            tags = {};warnings.append({'message': 'ExifTool: ' + str(exc)})
        found, duplicates = {}, 0
        for index, (source, path) in enumerate(locations):
            cancel()
            self.lab.update(jid, stage='Metadata / checksum · ' + path.name, done=index, total=len(locations))
            try:
                before = path.stat()
                digest = sha256(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):raise ValueError('Файл менялся во время анализа')
                if digest in found:
                    if found[digest]['device_id']!=source['device_id']:warnings.append({'message':'Одинаковый файл найден у разных устройств: '+path.name+'; сохранено первое назначение'})
                    found[digest]['locations'].append(str(path));duplicates += 1
                    continue
                old = previous.get(digest)
                if old and old.get('bytes') == before.st_size and old.get('mtime_ns') == before.st_mtime_ns and old.get('parser_version') == 'exiftool-ffprobe-v001':
                    asset = deepcopy(old)
                else:
                    asset = extract(path, tags.get(str(path.resolve()).casefold(), {}), config, devices[source['device_id']], cancel)
                original = Path(origins.get(digest, str(path)))
                asset.update(asset_id=digest, sha256=digest, path=str(path), source_ref=source['device_id'] + '/' + path.relative_to(source['root']).as_posix(),
                             device_id=source['device_id'], source_group=str(original.parent).casefold(),
                             locations=[str(path)], available=True)
                found[digest] = asset
            except (OSError, ValueError) as exc:
                aid = 'unreadable-' + stable(str(path))
                found[aid] = {'asset_id': aid, 'name': path.name, 'stem': path.stem, 'extension': path.suffix.lower(),
                              'media_type': 'video' if path.suffix.lower() in ('.mov','.mp4','.m4v','.webm') else 'photo',
                              'bytes': path.stat().st_size if path.exists() else 0, 'path': str(path), 'device_id': source['device_id'],
                              'source_ref': source['device_id']+'/'+path.name, 'source_group':str(path.parent), 'raw_timestamp':None,
                              'warnings':[str(exc)], 'available':False, 'locations':[str(path)]}
        for aid, old in previous.items():
            if aid not in found and old.get('parent_asset_id') is None:
                old = deepcopy(old);old['available'] = False;old['warnings'] = list(old.get('warnings', [])) + ['Источник offline/missing; решение отбора сохранено']
                found[aid] = old
        for derived in state.get('derivatives', []):found[derived['asset_id']] = derived
        with self.e.conn() as c:
            for aid, asset in found.items():
                c.execute('INSERT OR REPLACE INTO ml_assets VALUES(?,?,?)', (sid, aid, json.dumps(asset, ensure_ascii=False)))
        self.lab.update(jid, done=len(locations))
        return self.rebuild(sid, jid, cancel, len(locations), duplicates, warnings)

    def rebuild(self, sid, jid, cancel, physical=None, duplicates=None, warnings=None):
        state, revision = self.state(sid)
        with self.e.conn() as c:
            raw = [json.loads(r['data']) for r in c.execute('SELECT data FROM ml_assets WHERE session_id=? ORDER BY asset_id', (sid,))]
        if physical is None:
            old = self.latest(sid)
            physical = old['stats']['source_file_count'] if old else sum(len(a.get('locations', [a['path']])) for a in raw)
            duplicates = old['stats']['duplicate_count'] if old else 0
        cancel()
        self.lab.update(jid, stage='Хронология / PHOTO ACTIVITY')
        result = build(raw, state['config'], state['review'], physical, duplicates or 0)
        result['stats']['warnings'].extend(warnings or [])
        result['stats']['quality']['analysis_complete'] = not result['stats']['warnings']
        aid = {a['asset_id']: a for a in result['assets']}
        result['markers'] = [{**m, 'corrected_time_utc': aid.get(m['asset_id'], {}).get('corrected_start_utc')} for m in state['review'].get('markers', [])]
        validate_result(result)
        validate('markers', {'markers': result['markers']})
        analysis_revision = (state['latest_analysis'] or 0) + 1
        result['analysis_revision'] = analysis_revision
        result['input_config_fingerprint'] = stable({'assets': [(a['asset_id'], a.get('raw_timestamp')) for a in raw],
                                                      'config': state['config'], 'review': state['review'],'prepared':state.get('prepared',{})})
        root = self.folder(sid) / 'DATA';root.mkdir(parents=True, exist_ok=True)
        temp = root / ('.r' + str(analysis_revision) + '-' + uuid.uuid4().hex + '.partial')
        outputs(temp, state['config'], result, analysis_revision)
        cancel()
        final = root / (f'r{analysis_revision:06d}-' + uuid.uuid4().hex[:6])
        temp.rename(final)
        result['folder'] = str(final)
        state['latest_analysis'] = analysis_revision
        with self.e.lock, self.e.conn() as c:
            current = c.execute('SELECT revision FROM ml_projects WHERE session_id=?', (sid,)).fetchone()[0]
            if current != revision:raise ValueError('Параметры изменились во время анализа; пересчитайте')
            c.execute('INSERT INTO ml_analyses VALUES(?,?,?,?)', (sid, analysis_revision, json.dumps(result, ensure_ascii=False), str(final)))
            c.execute('UPDATE ml_projects SET revision=?,data=? WHERE session_id=?', (revision + 1, json.dumps(state, ensure_ascii=False), sid))
        atomic_json(root / 'current.json', {'analysis_revision': analysis_revision, 'directory': final.name})
        self.e.offline.dirty.add(sid)
        return {'analysis_revision': analysis_revision, 'folder': str(final), 'captures': len(result['captures']),
                'warnings': len(result['stats']['warnings']), 'complete': result['stats']['quality']['analysis_complete']}

    def source(self, asset):
        from lab_workspace import sha256
        paths = [asset['path']] + asset.get('locations', [])
        for name in paths:
            path = Path(name)
            if path.is_file() and path.stat().st_size == asset['bytes'] and sha256(path) == asset.get('sha256'):
                return path
        raise ValueError('Исходник недоступен или изменился: ' + asset['name'])

    def export(self, sid, operation, body, jid, cancel):
        from lab_workspace import sha256
        result = self.latest(sid)
        if not result:raise ValueError('Сначала выполните Analyze')
        validate_result(result)
        state, _ = self.state(sid)
        if result['input_config_fingerprint'] != stable({'assets': [(a['asset_id'], a.get('raw_timestamp')) for a in self.raw_assets(sid)],
                                                        'config': state['config'], 'review': state['review'],'prepared':state.get('prepared',{})}):
            self.rebuild(sid,jid,cancel)
            result=self.latest(sid);state,_=self.state(sid)
        final = operation == 'final'
        items = [r for r in result['timeline']['items'] if r['media_type'] in ('photo', 'video') and (state['review']['selection'].get(r['asset_id']) == 'keep' if final else state['review']['selection'].get(r['asset_id']) != 'reject')]
        if not items:raise ValueError('Нет выбранных PHOTO/VIDEO' if final else 'Хронология пуста')
        byid = {a['asset_id']: a for a in result['assets']}
        plan = []
        unit_state, _ = self.unit.state(sid)
        for index, row in enumerate(items, 1):
            cancel()
            asset = byid[row['asset_id']]
            if row['media_type'] == 'photo' and row.get('jpeg_asset_id'):
                asset = byid[row['jpeg_asset_id']]
            source = self.source(asset)
            if final and row['media_type'] == 'photo':
                prepared = state.get('prepared',{}).get(row['asset_id']) or unit_state.get('jpeg', {}).get(byid[row['asset_id']]['path'])
                if prepared:source = self.unit.checked(prepared, sid)
                elif row.get('is_raw') and not row.get('jpeg_asset_id'):raise ValueError('Подключите обработанный JPEG: ' + row['name'])
            ext = '.jpg' if final and row['media_type'] == 'photo' else '.mp4' if final else source.suffix.lower()
            if final and row['media_type'] == 'video':
                info, errors = probe(source, state['config'], cancel)
                video = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), {})
                audio = [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']
                if errors or video.get('codec_name') != 'h264' or any(s.get('codec_name') != 'aac' for s in audio):
                    raise ValueError('Для финала нужен подготовленный H.264/AAC MP4: ' + row['name'])
                row={**row,'needs_remux':source.suffix.lower()!='.mp4' or 'mp4' not in str(info.get('format',{}).get('format_name',''))}
            if final:
                name = f'{index:0{max(3,len(str(len(items))))}d}' + ext
            elif body.get('naming') == 'sequence' or state['review']['manual_order']:
                name = f'{index:0{max(3,len(str(len(items))))}d}' + ext
            else:
                name = str(Path(row['working_filename']).with_suffix(ext))
            plan.append((row, source, name))
        if len({name.casefold() for _, _, name in plan}) != len(plan):raise ValueError('Конфликт рабочих имён; выберите последовательную нумерацию')
        root = self.folder(sid) / ('EXPORTS' if final else 'WORK')
        root.mkdir(parents=True, exist_ok=True)
        estimated = sum(p.stat().st_size for _, p, _ in plan) + 20 * 1024 * 1024
        if shutil.disk_usage(root).free < estimated:raise ValueError('Недостаточно места для проверяемых копий')
        version = 1
        while (root / f'v{version:03d}').exists():version += 1
        staging = root / (f'.v{version:03d}-' + uuid.uuid4().hex + '.partial');staging.mkdir()
        media = staging / 'CLIENT' / 'MEDIA' if final else staging / 'TIMELINE';media.mkdir(parents=True)
        manifest = []
        self.lab.update(jid, stage='Финальная последовательность' if final else 'Рабочая последовательность', total=len(plan), done=0)
        for index, (row, source, name) in enumerate(plan):
            cancel();destination = media / name;before = sha256(source)
            if final and row['media_type'] == 'video' and row.get('needs_remux'):
                exe = tool('ffmpeg', state['config'])
                if not exe:raise ValueError('Нужен FFmpeg для настоящего MP4 remux')
                command([exe, '-v', 'error', '-n', '-i', source, '-map', '0:v:0', '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart', destination], cancel, 600)
            elif final and row['media_type'] == 'photo':
                from PIL import Image, ImageCms, ImageOps
                import io
                with Image.open(source) as im:
                    profile=im.info.get('icc_profile');profile_name=ImageCms.getProfileName(ImageCms.ImageCmsProfile(io.BytesIO(profile))) if profile else ''
                    if im.format == 'JPEG' and im.getexif().get(274, 1) == 1 and 'srgb' in profile_name.lower():
                        shutil.copy2(source, destination)
                    else:
                        image = ImageOps.exif_transpose(im).convert('RGB')
                        icc = im.info.get('icc_profile')
                        if icc:image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile('sRGB'))
                        image.save(destination, 'JPEG', quality=95, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
                with Image.open(destination) as image:
                    if image.format != 'JPEG':raise ValueError('Выход не является JPEG')
                    image.verify()
            else:
                shutil.copy2(source, destination)
                if sha256(destination) != before:raise ValueError('Checksum копии не совпала')
            if sha256(source) != before:raise ValueError('Исходник менялся во время экспорта')
            manifest.append({'index': index + 1, 'item_id': row['item_id'], 'asset_id': row['asset_id'],
                             'file': name, 'source_ref': row['source_ref'], 'corrected_start_utc': row['corrected_start_utc'],
                             'trim_in_seconds': row.get('trim_in_seconds'), 'trim_out_seconds': row.get('trim_out_seconds'),
                             'sha256': sha256(destination), 'bytes': destination.stat().st_size})
            self.lab.update(jid, done=index + 1)
        cancel()
        atomic_json(staging / 'manifest.json', {'schema_version': 'unit.multilayer.export/1', 'analysis_revision': result['analysis_revision'],
                                               'kind': operation, 'items': manifest, 'bridge_verified': False})
        if final:
            atomic_json(staging / 'CLIENT' / 'event.public.json', {'schema_version': 'unit.multilayer.public/1',
                        'title': state['config']['title'], 'event_id': state['config']['event_id'],
                        'media': [{'file': 'MEDIA/' + i['file'], 'sha256': i['sha256']} for i in manifest]})
        target = root / f'v{version:03d}';staging.rename(target)
        return {'folder': str(target), 'files': len(manifest), 'kind': operation, 'bridge_verified': False}

    def prepared_photo(self,sid,body,revision):
        if self.busy(sid):raise ValueError('Дождитесь задачи')
        state,current=self.state(sid)
        if current!=revision:raise ValueError('Проект изменился')
        assets={a['asset_id']:a for a in self.raw_assets(sid)};aid=body['asset_id']
        if aid not in assets or assets[aid]['media_type']!='photo':raise ValueError('Выберите исходную фотографию')
        path=Path(body['path']).resolve()
        from PIL import Image
        with Image.open(path) as image:
            if image.format!='JPEG':raise ValueError('Нужен настоящий JPEG')
            image.verify()
        if path==Path(assets[aid]['path']):raise ValueError('Свяжите отдельный обработанный JPEG')
        state.setdefault('prepared',{})[aid]=self.unit.file_record(path)
        self.commit(sid,state,revision)
        return {'revision':revision+1}

    def raw_assets(self, sid):
        with self.e.conn() as c:
            return [json.loads(r['data']) for r in c.execute('SELECT data FROM ml_assets WHERE session_id=? ORDER BY asset_id', (sid,))]

    def derivative(self, sid, body, revision):
        if self.busy(sid):raise ValueError('Дождитесь задачи')
        state, current = self.state(sid)
        if current != revision:raise ValueError('Проект изменился')
        assets = {a['asset_id']: a for a in self.raw_assets(sid)}
        parent = assets.get(body.get('parent_asset_id'))
        if not parent or parent['media_type'] != 'video':raise ValueError('Выберите исходный ролик')
        a, b = float(body['trim_in_seconds']), float(body['trim_out_seconds'])
        if parent.get('capture_duration_seconds') is None or not 0 <= a < b <= parent['capture_duration_seconds']:raise ValueError('Неверный trim')
        path = Path(body['path']).resolve()
        if not path.is_file() or path.suffix.lower() not in ('.mov', '.mp4'):raise ValueError('Выберите подготовленный MOV/MP4')
        if path == Path(parent['path']):raise ValueError('Нужен отдельный производный файл')
        from lab_workspace import sha256
        aid = sha256(path)
        if aid in assets:raise ValueError('Этот файл уже зарегистрирован')
        data = extract(path, {}, state['config'], {}, lambda: None)
        data.update(asset_id=aid, sha256=aid, path=str(path), locations=[str(path)], source_ref=parent['device_id']+'/'+path.name,
                    device_id=parent['device_id'], parent_asset_id=parent['asset_id'], trim_in_seconds=a, trim_out_seconds=b,
                    source_group=parent['source_group'], available=True)
        if data.get('capture_duration_seconds') is None or abs(data['capture_duration_seconds'] - (b - a)) > 1.5:
            raise ValueError('Длительность производного файла не совпала с trim range')
        state['derivatives'].append(data)
        self.commit(sid, state, revision)
        with self.e.conn() as c:c.execute('INSERT INTO ml_assets VALUES(?,?,?)', (sid, aid, json.dumps(data, ensure_ascii=False)))
        return {'revision': revision + 1, 'asset_id': aid}

    def reconcile(self, sid, folder):
        root = Path(folder).resolve()
        allowed = self.folder(sid) / 'WORK'
        if allowed not in root.parents:raise ValueError('Выберите собственную рабочую версию этого проекта')
        manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
        return {'missing': [i for i in manifest['items'] if not (root / 'TIMELINE' / i['file']).is_file()],
                'note': 'Отсутствие копии не меняет отбор. Подтвердите исключение отдельно.'}

    def import_event(self, sid, path, revision):
        original = Path(path).expanduser().resolve()
        incoming = json.loads(original.read_text('utf-8-sig'))
        if not isinstance(incoming, dict):raise ValueError('EVENT.json должен быть объектом')
        state, current = self.state(sid)
        if current != revision or self.busy(sid):raise ValueError('Проект изменился или занят')
        # Preserve the complete imported record privately; never write to the external EVENT.json.
        state['config']['generator_event'] = incoming
        for key in ('event_id', 'shoot_id', 'title', 'location', 'date'):
            if key in incoming:state['config'][key] = incoming[key]
        if incoming.get('record_id'):
            state['config']['record_id'] = str(incoming['record_id'])
        validate('event', state['config'])
        self.commit(sid, state, revision)
        return {'revision': revision + 1, 'source_unchanged': True}

    def link_uls(self, sid, record_id, revision):
        state, current = self.state(sid)
        if current != revision or self.busy(sid):raise ValueError('Проект изменился или занят')
        path = Path('E:/UNIT001/DATA/ULS/uls.sqlite3')
        if not path.is_file():raise ValueError('Общая база ULS недоступна')
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            row = conn.execute('SELECT record_id,shoot_id FROM shoot_ids WHERE record_id=?', (record_id,)).fetchone()
            if not row:raise ValueError('ID не найден в реестре ULS; автоматическое сопоставление запрещено')
        finally:conn.close()
        state['config'].update(record_id=row[0], uls_record_id=row[0], shoot_id=row[1])
        self.commit(sid, state, revision)
        return {'revision': revision + 1, 'uls_read_only': True, 'shoot_id': row[1]}

    def file(self, sid, name):
        result = self.latest(sid)
        if not result or name not in ('stats.json','activity.csv','activity.svg','activity.png','UNIT_REPORT.txt','assets.json','timeline.json','markers.json','EVENT.json'):
            raise ValueError('Файл отчёта не найден')
        return Path(result['folder']) / name

    def serve(self, handler, query):
        sid=query.get('session','');self.lab.validate(sid)
        aid=query.get('asset','')
        with self.e.conn() as c:row=c.execute('SELECT data FROM ml_assets WHERE session_id=? AND asset_id=?',(sid,aid)).fetchone()
        if not row:raise ValueError('Материал не зарегистрирован')
        asset=json.loads(row['data']);path=Path(asset['path'])
        if not path.is_file() or path.stat().st_size!=asset['bytes'] or path.stat().st_mtime_ns!=asset.get('mtime_ns'):
            raise ValueError('Исходник отсутствует или изменился')
        state,_=self.state(sid)
        if query.get('preview'):
            import io
            from PIL import Image,ImageOps
            cache=self.folder(sid)/'CACHE';cache.mkdir(parents=True,exist_ok=True)
            thumb=cache/(aid+'.jpg')
            if not thumb.exists():
                if asset['media_type']=='photo':
                    if asset.get('is_raw'):
                        exe=tool('exiftool',state['config'])
                        if not exe:raise ValueError('Нужен ExifTool для RAW-превью')
                        args=cache/(aid+'.args')
                        args.write_text('-b\n-PreviewImage\n'+str(path)+'\n','utf-8')
                        data=command([exe,'-charset','filename=UTF8','-@',args],timeout=30)
                        if not data:
                            args.write_text('-b\n-JpgFromRaw\n'+str(path)+'\n','utf-8')
                            data=command([exe,'-charset','filename=UTF8','-@',args],timeout=30)
                        image=Image.open(io.BytesIO(data))
                        own=image.getexif().get(274);image=ImageOps.exif_transpose(image)
                        if not own and asset.get('orientation') in (3,6,8):image=image.transpose({3:Image.Transpose.ROTATE_180,6:Image.Transpose.ROTATE_270,8:Image.Transpose.ROTATE_90}[asset['orientation']])
                    else:image=ImageOps.exif_transpose(Image.open(path))
                    image.thumbnail((1280,1280));temp=cache/(aid+'.'+uuid.uuid4().hex+'.partial.jpg');image.convert('RGB').save(temp,'JPEG',quality=86);temp.replace(thumb)
                elif asset['media_type']=='video':
                    exe=tool('ffmpeg',state['config'])
                    if not exe:raise ValueError('Нет FFmpeg для poster')
                    temp=cache/(aid+'.'+uuid.uuid4().hex+'.partial.jpg')
                    command([exe,'-v','error','-y','-i',path,'-frames:v','1','-vf','scale=640:-2',temp],timeout=30);temp.replace(thumb)
                else:raise ValueError('У аудио нет изображения')
            return handler.send(200,thumb.read_bytes(),'image/jpeg')
        import re,mimetypes
        if asset['media_type']=='photo':raise ValueError('Для фото используйте preview')
        size=path.stat().st_size;start,end=0,size-1
        header=handler.headers.get('Range')
        if header:
            match=re.fullmatch(r'bytes=(\d+)-(\d*)',header)
            if not match:return handler.send(416,{'error':'Неверный диапазон'})
            start=int(match[1]);end=min(int(match[2]) if match[2] else end,end)
        if start>end:return handler.send(416,{'error':'Неверный диапазон'})
        handler.send_response(206 if header else 200);handler.send_header('Content-Type',mimetypes.guess_type(path)[0] or 'application/octet-stream')
        handler.send_header('Accept-Ranges','bytes');handler.send_header('Content-Length',str(end-start+1))
        if header:handler.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        handler.end_headers()
        try:
            with path.open('rb') as stream:
                stream.seek(start);left=end-start+1
                while left:
                    block=stream.read(min(left,65536))
                    if not block:break
                    handler.wfile.write(block);left-=len(block)
        except (ConnectionError,OSError):pass

    def post(self, route, body):
        sid = body['session']
        if route == 'config':return self.save(sid, body['config'], int(body['revision']), True)
        if route == 'review':return self.save(sid, body['review'], int(body['revision']))
        if route == 'undo':
            state, revision = self.state(sid)
            if revision != int(body['revision']) or self.busy(sid):raise ValueError('Проект изменился или занят')
            if not state['history']:raise ValueError('Нет действий для отмены')
            old = state['history'].pop();state.update(old);self.commit(sid,state,revision);return {'revision':revision+1}
        if route == 'derivative':return self.derivative(sid,body,int(body['revision']))
        if route == 'prepared-photo':return self.prepared_photo(sid,body,int(body['revision']))
        if route == 'import-event':return self.import_event(sid,body['path'],int(body['revision']))
        if route == 'link-uls':return self.link_uls(sid,str(body['record_id']),int(body['revision']))
        if route == 'reconcile':return self.reconcile(sid,body['folder'])
        if route in ('analyze','sync','working','final'):return self.enqueue(sid,route,body)
        raise ValueError('Неизвестная команда Multi-Layer')
