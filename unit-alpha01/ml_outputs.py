"""Versioned JSON/CSV/text and one canonical SVG, rasterised with its own SVG subset."""
import csv
import io
import json
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temp.replace(path)


def raster_svg(svg, destination):
    from PIL import Image, ImageDraw, ImageFont
    tree = ET.fromstring(svg)
    factor = 2
    image = Image.new('RGB', (int(tree.attrib['width']) * factor, int(tree.attrib['height']) * factor), '#f7f7f2')
    draw = ImageDraw.Draw(image)
    fontfile = Path('C:/Windows/Fonts/segoeui.ttf')
    font = ImageFont.truetype(str(fontfile), 14 * factor) if fontfile.exists() else ImageFont.load_default(size=14 * factor)
    for node in tree:
        tag = node.tag.rsplit('}', 1)[-1]
        a = node.attrib
        if tag in ('polyline', 'polygon'):
            points = [tuple(float(v) * factor for v in p.split(',')) for p in a['points'].split()]
            if tag == 'polygon':
                draw.polygon(points, fill=a['fill'])
            elif len(points) > 1:
                draw.line(points, fill=a['stroke'], width=factor)
            elif points:
                x, y = points[0];draw.ellipse((x-factor, y-factor, x+factor, y+factor), fill=a['stroke'])
        elif tag == 'line':
            draw.line(tuple(float(a[k]) * factor for k in ('x1', 'y1', 'x2', 'y2')), fill=a['stroke'], width=factor)
        elif tag == 'text':
            anchor = {'start': 'ls', 'middle': 'ms', 'end': 'rs'}[a.get('text-anchor', 'start')]
            draw.text((float(a['x']) * factor, float(a['y']) * factor), node.text or '', font=font, fill='#191919', anchor=anchor)
    image.resize((int(tree.attrib['width']), int(tree.attrib['height'])), Image.Resampling.LANCZOS).save(destination)


def outputs(folder, event, result, revision):
    from zoneinfo import ZoneInfo
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    stats = {**result['stats'], 'analysis_revision': revision}
    timeline = {'schema_version': 'unit.multilayer.timeline/1', 'revision': revision, **result['timeline']}
    # Private files may contain local source references; client export uses a separate whitelist.
    atomic_json(folder / 'EVENT.json', {**event, 'schema_version': 'unit.multilayer.event/1', 'analysis_revision': revision})
    atomic_json(folder / 'assets.json', {'schema_version': 'unit.multilayer.assets/1', 'analysis_revision': revision, 'assets': result['assets']})
    atomic_json(folder / 'timeline.json', timeline)
    atomic_json(folder / 'markers.json', {'schema_version': 'unit.multilayer.markers/1', 'analysis_revision': revision, 'markers': result.get('markers', [])})
    atomic_json(folder / 'stats.json', stats)
    bins = stats['activity']['bins']
    csvfile = folder / 'activity.csv'
    with csvfile.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['revision', 'start_utc', 'end_utc', 'count', 'actual_duration_seconds', 'rate_per_minute', 'partial'])
        for b in bins:
            writer.writerow([revision] + [b[k] for k in ('start_utc', 'end_utc', 'count', 'actual_duration_seconds', 'rate_per_minute', 'partial')])
    width, height = 1200, 300
    left, right, top, bottom = 48, 1152, 70, 244
    rates = [b['rate_per_minute'] or 0 for b in bins]
    maximum = max(rates, default=0) or 1
    def dt(text):return datetime.fromisoformat(text.replace('Z', '+00:00'))
    begin, end = stats['event_start_utc'], stats['event_end_utc']
    duration = stats['duration_seconds'] or 0
    points = []
    for b, rate in zip(bins, rates):
        middle = dt(b['start_utc']) + (dt(b['end_utc']) - dt(b['start_utc'])) / 2
        x = left + (middle - dt(begin)).total_seconds() / duration * (right - left) if duration else (left + right) / 2
        points.append((round(x, 2), round(bottom - rate / maximum * (bottom - top), 2)))
    values = ' '.join(f'{x},{y}' for x, y in points)
    area = f'{left},{bottom} ' + values + f' {right},{bottom}'
    zone = ZoneInfo(event['timezone'])
    labels = [('UNIT001 / PHOTO ACTIVITY', 48, 28, 'start'),
              (f"{stats['activity']['interval_minutes']} MIN / {stats['activity']['scope']} / PHOTOS PER MIN / R{revision}", 48, 51, 'start')]
    if begin:
        labels.extend([(dt(begin).astimezone(zone).isoformat(timespec='minutes'), left, 278, 'start'),
                       (dt(end).astimezone(zone).isoformat(timespec='minutes'), right, 278, 'end')])
    peak = stats['activity']['peak_count_bin_index']
    if peak is not None:
        b = bins[peak]
        labels.append((f"PEAK COUNT {b['count']} / {dt(b['start_utc']).astimezone(zone).strftime('%H:%M')}–{dt(b['end_utc']).astimezone(zone).strftime('%H:%M')}", right, 28, 'end'))
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" style="background:#f7f7f2" role="img" aria-label="Photo activity">'
    svg += f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#b0b0ab"/>'
    if points:
        svg += f'<polygon points="{area}" fill="#e3e3dc"/><polyline points="{values}" fill="none" stroke="#191919"/>'
    for text, x, y, anchor in labels:
        svg += f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="Segoe UI,sans-serif" font-size="14" font-weight="400">{escape(text)}</text>'
    svg += '</svg>'
    (folder / 'activity.svg').write_text(svg, 'utf-8')
    raster_svg(svg, folder / 'activity.png')
    def local(value):return dt(value).astimezone(zone).isoformat() if value else 'N/A'
    def number(value):return f'{value:.2f}' if value is not None else 'N/A'
    report = [f"UNIT001 / EVENT {event.get('event_id') or '—'}", f'ANALYSIS REVISION {revision}', '',
              'START           ' + local(begin), 'END             ' + local(end),
              'DURATION SEC    ' + number(stats['duration_seconds']), '',
              f"PHOTOS          {stats['capture_count']}", f"VIDEOS          {stats['video_source_count']}",
              f"MEDIA ASSETS    {stats['media_asset_count']}", f"SOURCE FILES    {stats['source_file_count']}",
              'AVG / MIN       ' + number(stats['photos_per_minute']), 'AVG / HOUR      ' + number(stats['photos_per_hour']),
              '', 'PHOTO ACTIVITY  ' + stats['activity']['scope'], f"INTERVAL        {stats['activity']['interval_minutes']} MIN", '']
    for b in bins:
        report.append(f"{local(b['start_utc'])} — {local(b['end_utc'])}   {b['count']}   {number(b['rate_per_minute'])}/min")
    report += ['', f"WARNINGS        {len(stats['warnings'])}"]
    report += [w.get('message', '') for w in stats['warnings']]
    (folder / 'UNIT_REPORT.txt').write_text('\n'.join(report), 'utf-8')
    return stats
