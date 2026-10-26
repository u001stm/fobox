"""Deterministic Multi-Layer time, captures, anchors and activity. No filesystem writes."""
from bisect import bisect_right, bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

UTC = timezone.utc
VERSION = 'unit.multilayer/1'


def stable(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def iso(value):
    return value.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z') if value else None


def instant(value, zone, allow_naive=True):
    if not value:
        return None, False
    text = re.sub(r'^(\d{4}):(\d\d):(\d\d)', r'\1-\2-\3', str(value).strip())
    date = datetime.fromisoformat(text.replace('Z', '+00:00'))
    assumed = date.tzinfo is None
    if assumed:
        if not allow_naive:
            raise ValueError('У времени не указана зона')
        tz = ZoneInfo(zone)
        a, b = date.replace(tzinfo=tz, fold=0), date.replace(tzinfo=tz, fold=1)
        if a.utcoffset() != b.utcoffset():
            raise ValueError('Неоднозначное или несуществующее местное время; укажите UTC offset')
        if a.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != date:
            raise ValueError('Несуществующее местное время')
        date = a
    return date.astimezone(UTC), assumed


def corrected(asset, device, config, review):
    warnings = list(asset.get('warnings', []))
    raw = review.get('time_overrides', {}).get(asset['asset_id']) or asset.get('raw_timestamp')
    source = 'manual_device_time' if asset['asset_id'] in review.get('time_overrides', {}) else asset.get('time_source')
    if raw and device.get('quicktime_utc') and str(source).startswith('QuickTime:') and not re.search(r'(Z|[+-]\d\d:\d\d)$',str(raw)):
        raw=str(raw)+'Z'
    try:
        value, assumed = instant(raw, device.get('timezone') or config['timezone'], config.get('allow_naive', True))
    except (ValueError, OverflowError) as exc:
        return {**asset, 'normalized_start_utc': None, 'corrected_start_utc': None, 'corrected_end_utc': None,
                'warnings': warnings + [str(exc)], 'time_source': source, 'time_confidence': 'unknown'}
    if value is None:
        warnings.append('Нет времени съёмки; оставлено в UNPLACED')
    if assumed:
        warnings.append('Timezone assumed: ' + (device.get('timezone') or config['timezone']))
    if value and asset.get('time_candidates') and source!='manual_device_time':
        for key,candidate in asset['time_candidates']:
            try:
                if device.get('quicktime_utc') and key.startswith('QuickTime:') and not re.search(r'(Z|[+-]\d\d:\d\d)$',candidate):candidate+='Z'
                other,_=instant(candidate,device.get('timezone') or config['timezone'],config.get('allow_naive',True))
                if other and abs((other-value).total_seconds())>2:
                    warnings.append('Конфликт временных тегов: '+str(source)+' / '+key);break
            except ValueError:pass
    offset = float(device.get('offset_seconds', 0))
    if not math.isfinite(offset) or abs(offset) > 86400 * 7:
        raise ValueError('Поправка устройства должна быть в пределах ±7 суток')
    start = value + timedelta(seconds=offset) if value else None
    duration = asset.get('capture_duration_seconds')
    if asset['media_type'] in ('video', 'audio') and duration is None:
        warnings.append('Неизвестна длительность: END и coverage неполные')
    end = start + timedelta(seconds=duration or 0) if start else None
    return {**asset, 'normalized_start_utc': iso(value), 'corrected_start_utc': iso(start),
            'corrected_end_utc': iso(end), 'offset_seconds_applied': offset, 'timezone_assumed': assumed,
            'time_source': source, 'time_confidence': 'assumed' if assumed else 'capture_tag' if value else 'unknown',
            'warnings': warnings}


def captures(assets):
    """Pair only unique RAW/JPEG candidates in the same source/device/stem/time/camera."""
    buckets = defaultdict(list)
    for a in assets:
        if a['media_type'] == 'photo':
            key = (a['device_id'], a.get('source_group'), a.get('stem', '').casefold(),
                   a.get('normalized_start_utc') or a.get('raw_timestamp'))
            buckets[key].append(a)
    out = []
    for group in buckets.values():
        raws = [a for a in group if a.get('is_raw')]
        jpegs = [a for a in group if a.get('extension') in ('.jpg', '.jpeg')]
        # A missing timestamp or ambiguous stem is not evidence of one exposure.
        compatible = not any(raws[0].get(k) and jpegs[0].get(k) and raws[0][k] != jpegs[0][k] for k in ('camera','serial')) if len(raws)==len(jpegs)==1 else False
        pairs = [group] if len(raws) == len(jpegs) == 1 and len(group) == 2 and group[0].get('raw_timestamp') and compatible else [[a] for a in group]
        for pair in pairs:
            primary = next((a for a in pair if a.get('is_raw')), pair[0])
            ids = sorted(a['asset_id'] for a in pair)
            out.append({**primary, 'capture_id': stable(ids), 'asset_ids': ids,
                        'paired': len(pair) == 2,
                        'jpeg_asset_id': next((a['asset_id'] for a in pair if a.get('extension') in ('.jpg', '.jpeg')), None),
                        'pair_warning': 'Неоднозначная RAW/JPEG пара' if len(group) > len(pair) else None})
    return out


def union_seconds(ranges):
    ranges = sorted((a, b) for a, b in ranges if a and b and a < b)
    seconds, end = 0., None
    for a, b in ranges:
        if end is None or a > end:
            seconds += (b - a).total_seconds()
        elif b > end:
            seconds += (b - end).total_seconds()
        end = max(end, b) if end else b
    return seconds


def timeline(photo_captures, assets, devices, review, mode='all'):
    decisions = review.get('selection', {})
    def key(a):
        return (a['corrected_start_utc'], int(devices[a['device_id']].get('priority', 0)),
                (0,int(a['capture_sequence'])) if a.get('capture_sequence') is not None else (1,a.get('name','')), a.get('capture_id') or a['asset_id'])
    photos = [c for c in photo_captures if devices[c['device_id']]['role'] == 'MASTER_PHOTO'
              and c.get('corrected_start_utc') and (mode == 'all' or decisions.get(c['asset_id']) == 'keep')]
    photos.sort(key=key)
    times = [p['corrected_start_utc'] for p in photos]
    width = max(3, len(str(len(photos))))
    rows, gaps = [], defaultdict(list)
    for a in assets:
        if a['media_type'] not in ('video', 'audio') or not a.get('corrected_start_utc'):
            continue
        at = bisect_right(times, a['corrected_start_utc'])
        # Coverage is [start,end); an anchor preceding start is not covered.
        covered = [p['capture_id'] for p in photos[bisect_left(times,a['corrected_start_utc']):bisect_left(times,a.get('corrected_end_utc') or a['corrected_start_utc'])]]
        gaps[at].append({**a, 'item_id': 'asset:' + a['asset_id'], 'anchor_photo_id': photos[at - 1]['capture_id'] if at else None,
                         'anchor_index': at, 'covered_photo_ids': covered,
                         'trim_in_seconds': a.get('trim_in_seconds', 0), 'trim_out_seconds': a.get('trim_out_seconds', a.get('capture_duration_seconds'))})
    for index in range(len(photos) + 1):
        if index:
            p = photos[index - 1]
            rows.append({**p, 'item_id': 'capture:' + p['capture_id'], 'anchor_photo_id': None,
                         'working_filename': f'{index:0{width}d}' + ('.jpg' if p.get('jpeg_asset_id') or not p.get('is_raw') else p['extension'])})
        for n, item in enumerate(sorted(gaps[index], key=key), 1):
            rows.append({**item, 'working_filename': f'{index:0{width}d}-{n:02d}' + item['extension']})
    order = review.get('manual_order', [])
    known = {r['item_id']: r for r in rows}
    valid = [i for i in order if i in known]
    conflict = bool(order and (set(order) != set(known)))
    if order:
        rows = [known[i] for i in valid] + [r for r in rows if r['item_id'] not in set(valid)]
    for r in rows:
        r['selection'] = decisions.get(r['asset_id'], 'undecided')
        r['order_override'] = bool(order)
    return {'items': rows, 'unplaced': [a['asset_id'] for a in assets if not a.get('corrected_start_utc')],
            'no_photo_master': not bool(photos), 'manual_order_conflict': conflict,
            'ordering_policy': 'manual' if order else 'corrected_start_photo_first'}


def statistics(caps, assets, devices, config, source_file_count, duplicate_count=0):
    zone = ZoneInfo(config['timezone'])
    def dt(t):
        return datetime.fromisoformat(t.replace('Z', '+00:00')) if t else None
    starts = [dt(a['corrected_start_utc']) for a in assets if a.get('corrected_start_utc')]
    ends = [dt(a['corrected_end_utc']) for a in assets if a.get('corrected_end_utc')]
    observed_start, observed_end = min(starts, default=None), max(ends, default=None)
    start = instant(config['event_start'], config['timezone'])[0] if config.get('event_start') else observed_start
    end = instant(config['event_end'], config['timezone'])[0] if config.get('event_end') else observed_end
    if start and end and end < start:
        raise ValueError('Конец события раньше начала')
    seconds = (end - start).total_seconds() if start and end else None
    scope = [c for c in caps if config.get('activity_scope', 'master_photo_captures') == 'all_photo_devices'
             or devices[c['device_id']]['role'] == 'MASTER_PHOTO']
    timed = sorted(dt(c['corrected_start_utc']) for c in scope if c.get('corrected_start_utc'))
    inside = [t for t in timed if start <= t <= end] if start and end else []
    minutes = config.get('interval_minutes', 'AUTO')
    if minutes == 'AUTO':
        minutes = 5 if seconds is None or seconds <= 5400 else 10 if seconds <= 14400 else 30
    if minutes not in (5, 10, 30):
        raise ValueError('Интервал: AUTO / 5 / 10 / 30')
    bins = []
    if start and end:
        local = start.astimezone(zone)
        cursor = local.replace(minute=local.minute // minutes * minutes, second=0, microsecond=0).astimezone(UTC)
        while cursor < end or not bins:
            if len(bins)>20000:raise ValueError('Слишком большой временной диапазон; проверьте дату и границы события')
            nxt = cursor + timedelta(minutes=minutes)
            a, b = max(cursor, start), min(nxt, end)
            actual = (b - a).total_seconds()
            count = sum(a <= t < b or (b == end and t == end) for t in inside)
            bins.append({'start_utc': iso(a), 'end_utc': iso(b), 'label': a.astimezone(zone).isoformat(),
                         'nominal_duration_seconds': minutes * 60, 'actual_duration_seconds': actual,
                         'count': count, 'rate_per_minute': count * 60 / actual if actual else None,
                         'partial': actual != minutes * 60})
            cursor = nxt
    count_ties, rate_ties = [], []
    if seconds and bins and max(b['count'] for b in bins) > 0:
        maximum = max(b['count'] for b in bins)
        count_ties = [i for i, b in enumerate(bins) if b['count'] == maximum]
        full = [(i, b) for i, b in enumerate(bins) if not b['partial']]
        candidates = full or [(i, b) for i, b in enumerate(bins) if b['rate_per_minute'] is not None]
        highest = max((b['rate_per_minute'] for i, b in candidates), default=None)
        rate_ties = [i for i, b in candidates if b['rate_per_minute'] == highest]
    videos = [a for a in assets if a['media_type'] == 'video' and not a.get('parent_asset_id')]
    known = [a for a in videos if a.get('capture_duration_seconds') is not None]
    unknown = sum(a['media_type'] in ('video', 'audio') and a.get('capture_duration_seconds') is None for a in assets)
    warnings = [{'asset_id': a['asset_id'], 'message': w} for a in assets for w in a.get('warnings', [])]
    if len(timed)!=len(inside):warnings.append({'message':f'{len(timed)-len(inside)} capture вне ручных границ события'})
    return {'schema_version': 'unit.multilayer.stats/1', 'event_id': config.get('event_id') or None,
            'timezone': config['timezone'], 'event_start_utc': iso(start), 'event_end_utc': iso(end),
            'observed_start_utc': iso(observed_start), 'observed_end_utc': iso(observed_end),
            'photo_start_utc': iso(min(timed, default=None)), 'photo_end_utc': iso(max(timed, default=None)),
            'bounds_policy': 'manual' if config.get('event_start') or config.get('event_end') else 'observed',
            'duration_seconds': seconds, 'source_file_count': source_file_count, 'capture_count': len(caps),
            'raw_jpeg_pair_count': sum(c['paired'] for c in caps), 'duplicate_count': duplicate_count,
            'video_source_count': len(videos), 'media_asset_count': len(caps) + sum(a['media_type'] != 'photo' for a in assets),
            'activity_capture_count': len(inside), 'outside_event_capture_count': len(timed) - len(inside),
            'photos_per_minute': len(inside) * 60 / seconds if seconds else None,
            'photos_per_hour': len(inside) * 3600 / seconds if seconds else None,
            'video_duration_sum_seconds': sum(a['capture_duration_seconds'] for a in known),
            'video_coverage_seconds': union_seconds([(dt(a['corrected_start_utc']), dt(a['corrected_end_utc'])) for a in known]),
            'source_bytes': sum(a['bytes'] for a in assets),
            'activity': {'scope': config.get('activity_scope', 'master_photo_captures'), 'interval_minutes': minutes,
                         'mode': 'auto' if config.get('interval_minutes') == 'AUTO' else 'manual',
                         'alignment': 'event_local_clock', 'bins': bins,
                         'peak_count_bin_index': count_ties[0] if count_ties else None,
                         'peak_rate_bin_index': rate_ties[0] if rate_ties else None,
                         'peak_count_ties': count_ties, 'peak_rate_ties': rate_ties},
            'quality': {'untimed_asset_count': sum(not a.get('corrected_start_utc') for a in assets),
                        'unknown_duration_count': unknown, 'analysis_complete': not warnings}, 'warnings': warnings}


def build(raw_assets, config, review, source_file_count, duplicate_count=0):
    devices = {d['device_id']: d for d in config['devices']}
    if len(devices) != len(config['devices']) or not devices:
        raise ValueError('Нужны уникальные устройства')
    if sum(d['role'] == 'MASTER_PHOTO' for d in devices.values()) > 1:
        raise ValueError('Выберите одно основное фотоустройство')
    assets = [corrected(a, devices[a['device_id']], config, review) for a in raw_assets]
    byid = {a['asset_id']: a for a in assets}
    for a in assets:
        if a.get('parent_asset_id'):
            parent = byid.get(a['parent_asset_id'])
            if not parent:
                raise ValueError('Нет исходника производного клипа')
            inside, outside = a['trim_in_seconds'], a['trim_out_seconds']
            if parent.get('capture_duration_seconds') is None or not 0 <= inside < outside <= parent['capture_duration_seconds']:
                raise ValueError('Неверные границы производного клипа')
            base = parent.get('normalized_start_utc')
            a['raw_timestamp'] = iso(datetime.fromisoformat(base.replace('Z', '+00:00')) + timedelta(seconds=inside)) if base else None
            a.update(corrected(a, devices[parent['device_id']], config, review))
    caps = captures(assets)
    tl = timeline(caps, assets, devices, review, config.get('master_mode', 'all'))
    stats = statistics(caps, assets, devices, config, source_file_count, duplicate_count)
    if tl['no_photo_master']:
        stats['warnings'].append({'message': 'NO PHOTO MASTER'})
    for c in caps:
        if c.get('pair_warning'):
            stats['warnings'].append({'asset_id': c['asset_id'], 'message': c['pair_warning']})
    stats['quality']['analysis_complete'] = not stats['warnings']
    return {'assets': assets, 'captures': caps, 'timeline': tl, 'stats': stats}
