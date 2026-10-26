"""Read-only ExifTool / ffprobe adapters. Original grouped tags are retained privately."""
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from engine import RAW_EXT

PHOTO = RAW_EXT | {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.tif', '.tiff', '.webp'}
VIDEO = {'.mov', '.mp4', '.m4v', '.webm'}
AUDIO = {'.wav', '.mp3', '.m4a', '.flac', '.ogg'}
EXCLUDED = {'work', 'cache', 'exports', 'logs', 'data', '.git', 'node_modules', '$recycle.bin'}


class Cancelled(RuntimeError):
    pass


def tool(name, config):
    supplied = config.get('tools', {}).get(name)
    if supplied:return str(Path(supplied).resolve()) if Path(supplied).is_file() else None
    found=shutil.which(name)
    if found:return found
    local=Path.home()/'AppData'/'Local'
    candidates=[local/'Programs'/'ExifTool'/'ExifTool.exe'] if name=='exiftool' else list((local/'Microsoft'/'WinGet'/'Packages').glob('Gyan.FFmpeg*/ffmpeg*/bin/'+name+'.exe'))
    return str(next((p for p in candidates if p.is_file()),'')) or None


def command(args, check_cancel=lambda: None, timeout=120, allow_json_error=False):
    process = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    start = time.monotonic()
    try:
        while True:
            check_cancel()
            try:
                out, error = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - start > timeout:
                    raise ValueError('Истекло время выполнения ' + Path(str(args[0])).name)
        if process.returncode:
            if not (allow_json_error and out.lstrip().startswith(b'[')):
                raise ValueError(error.decode('utf-8', 'replace')[-1500:] or 'Ошибка внешнего инструмента')
        return out
    except BaseException:
        process.kill()
        process.communicate()
        raise


def inventory(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('Папка недоступна: ' + str(root))
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d.casefold() not in EXCLUDED and not Path(folder, d).is_symlink())
        for name in sorted(files):
            p = Path(folder, name)
            if not p.is_symlink() and p.suffix.lower() in PHOTO | VIDEO | AUDIO and not name.startswith('._'):
                yield p


def tags_for(paths, config, scratch, cancel):
    exe = tool('exiftool', config)
    if not exe:
        return {}, ['ExifTool не найден; время части файлов останется неизвестным']
    argument_file = Path(scratch) / 'exiftool-arguments.txt'
    paths = [p for p in paths if '\n' not in str(p) and '\r' not in str(p)]
    argument_file.write_text('\n'.join(str(p) for p in paths) + '\n', 'utf-8')
    args = [exe, '-charset', 'filename=UTF8', '-j', '-G1', '-n', '-a',
            '-DateTimeOriginal', '-SubSecDateTimeOriginal', '-SubSecTimeOriginal', '-OffsetTimeOriginal',
            '-CreateDate', '-CreationDate', '-DateCreated', '-MediaCreateDate', '-TrackCreateDate',
            '-Model', '-Make', '-SerialNumber', '-InternalSerialNumber', '-ImageUniqueID',
            '-ImageWidth', '-ImageHeight', '-Orientation', '-ISO', '-ExposureTime', '-FNumber',
            '-FocalLength', '-LensModel', '-Duration', '-GPSLatitude', '-GPSLongitude', '-GPSAltitude',
            '-@', argument_file]
    if not paths:
        return {}, []
    rows = json.loads(command(args, cancel, timeout=300, allow_json_error=True).decode('utf-8-sig'))
    out = {}
    for row in rows:
        source = row.get('SourceFile') or row.get('File:SourceFile')
        if source:
            out[str(Path(source).resolve()).casefold()] = row
    return out, []


def take(tags, *names):
    for name in names:
        if tags.get(name) is not None:
            return tags[name]
        if ':' not in name:
            for key, value in tags.items():
                if key.rsplit(':', 1)[-1] == name and value is not None:
                    return value
    return None


def probe(path, config, cancel=lambda: None):
    exe = tool('ffprobe', config)
    if not exe:
        return {}, ['ffprobe не найден: длительность/совместимость не проверены']
    try:
        result = json.loads(command([exe, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path], cancel).decode('utf-8'))
        return result, []
    except ValueError as exc:
        return {}, ['Media probe: ' + str(exc)]


def extract(path, tags, config, device, cancel=lambda: None):
    ext = path.suffix.lower()
    kind = 'photo' if ext in PHOTO else 'video' if ext in VIDEO else 'audio'
    warnings = []
    error = take(tags, 'Error')
    if error:warnings.append('ExifTool: ' + str(error))
    media, probe_warnings = probe(path, config, cancel) if kind != 'photo' else ({}, [])
    warnings.extend(probe_warnings)
    candidates = []
    keys = ('Composite:SubSecDateTimeOriginal', 'EXIF:DateTimeOriginal', 'ExifIFD:DateTimeOriginal', 'DateTimeOriginal') if kind == 'photo' else (
        'Keys:CreationDate', 'UserData:DateTimeOriginal', 'QuickTime:CreateDate', 'QuickTime:MediaCreateDate', 'QuickTime:TrackCreateDate')
    for key in keys:
        value = take(tags, key)
        if value and not str(value).startswith('0000'):
            candidates.append((key, str(value)))
    if kind != 'photo':
        for scope, data in [('format', media.get('format', {}))] + [('stream', s) for s in media.get('streams', [])]:
            if data.get('tags', {}).get('creation_time'):
                candidates.append(('ffprobe:' + scope + ':creation_time', data['tags']['creation_time']))
    if kind == 'photo' and not candidates:
        # PIL exposes standard JPEG EXIF but does not invent filesystem dates for RAW.
        try:
            from PIL import Image
            with Image.open(path) as image:
                exif = image.getexif()
                sub = exif.get_ifd(34665) if 34665 in exif else exif
                value = sub.get(36867)
                if value:
                    candidates.append(('PIL:DateTimeOriginal', str(value)))
        except Exception:
            pass
    selected_key, raw = candidates[0] if candidates else (None, None)
    if raw and kind == 'photo':
        subsec = take(tags, 'SubSecTimeOriginal')
        offset = take(tags, 'OffsetTimeOriginal')
        if subsec and not re.search(r'\d\d:\d\d:\d\d\.\d', raw):
            raw += '.' + str(subsec).lstrip('.')
        if offset and not re.search(r'(Z|[+-]\d\d:\d\d)$', raw):
            raw += str(offset)
    duration = None
    if kind != 'photo':
        values = [media.get('format', {}).get('duration')]
        values += [s.get('duration') for s in media.get('streams', [])]
        values += [take(tags, 'Duration')]
        numbers = []
        for value in values:
            try:
                number = float(value)
                if math.isfinite(number) and number >= 0:
                    numbers.append(number)
            except (ValueError, TypeError):
                pass
        duration = numbers[0] if numbers else None
        if numbers and max(numbers) - min(numbers) > 1:
            warnings.append('Длительность разных metadata-источников расходится')
    stream = next((s for s in media.get('streams', []) if s.get('codec_type') == 'video'), {})
    stat = path.stat()
    return {'name': path.name, 'stem': path.stem, 'extension': ext, 'media_type': kind, 'is_raw': ext in RAW_EXT,
            'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'raw_timestamp': raw, 'time_source': selected_key,
            'time_candidates': candidates, 'capture_duration_seconds': duration,
            'camera': str(take(tags, 'Model') or ''), 'serial': str(take(tags, 'SerialNumber', 'InternalSerialNumber') or ''),
            'capture_sequence': int(re.search(r'(\d+)$', path.stem)[1]) if re.search(r'(\d+)$', path.stem) else None,
            'width': stream.get('width') or take(tags, 'ImageWidth'), 'height': stream.get('height') or take(tags, 'ImageHeight'),
            'codec': stream.get('codec_name'), 'audio_codecs': [s.get('codec_name') for s in media.get('streams', []) if s.get('codec_type') == 'audio'],
            'format_name': media.get('format', {}).get('format_name'), 'fps': stream.get('avg_frame_rate'),
            'orientation': take(tags, 'Orientation'),
            'iso': take(tags, 'ISO'), 'shutter_seconds': take(tags, 'ExposureTime'), 'aperture': take(tags, 'FNumber'),
            'focal_length_mm': take(tags, 'FocalLength'), 'lens': take(tags, 'LensModel'),
            'gps': {'lat': take(tags, 'GPSLatitude'), 'lon': take(tags, 'GPSLongitude'), 'altitude': take(tags, 'GPSAltitude')},
            'metadata': tags, 'probe': media, 'warnings': warnings,
            'filesystem_time_diagnostic': stat.st_mtime, 'parser_version': 'exiftool-ffprobe-v001'}
