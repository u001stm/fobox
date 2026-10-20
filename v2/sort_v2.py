"""FOBOX 2.0: face-guided local RAW sorting; human review required."""
import argparse
import csv
import json
import math
import re
import shutil
import sys
from pathlib import Path
import cv2
import numpy as np

def index_files(folder, suffix, prefix):
    files = {}
    pattern = re.compile(re.escape(prefix) + r'0*(\d+)', re.I)
    for p in folder.iterdir():
        if p.suffix.lower() != suffix.lower():
            continue
        match = pattern.fullmatch(p.stem)
        if match:
            number = int(match.group(1))
            if number in files:
                raise ValueError(f'Duplicate frame {number}: {files[number]} and {p}')
            files[number] = p
    return files

def image_at(path):
    if path is None:
        return None
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)

def is_blank(image):
    small = cv2.cvtColor(cv2.resize(image, (64, 64)), cv2.COLOR_BGR2GRAY)
    return (small.mean() > 246 or small.mean() < 9) and small.std() < 13

def image_hash(image):
    gray = cv2.cvtColor(cv2.resize(image, (32, 32)), cv2.COLOR_BGR2GRAY)
    dct = cv2.dct(gray.astype(np.float32))[:8, :8]
    return dct > np.median(dct[1:, :])

def read_manifest(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not set(('Name','V','P1','P2','D1','D2')).issubset(reader.fieldnames or []):
            raise ValueError('CSV columns must be Name,V,P1,P2,D1,D2')
        result = []
        for row in reader:
            name = ' '.join(row['Name'].split()[:2])
            if not name or re.search(r'[\\/:*?"<>|]', name):
                raise ValueError(f'Invalid student name: {name}')
            try:
                numbers = [int(row[k]) for k in ('V','P1','P2','D1','D2')]
            except (TypeError, ValueError):
                print('SKIP incomplete row:', name)
                continue
            if len(set(numbers)) != 5:
                raise ValueError(f'Duplicate required frame for {name}')
            result.append((name, numbers))
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default=r'D:\DCIM\100MSDCF')
    parser.add_argument('--destination', default=r'F:\RAW_SORTED_V2')
    parser.add_argument('--manifest', default='students_example.csv')
    parser.add_argument('--prefix', help='3 Latin letters; auto-detected when unique')
    parser.add_argument('--radius', type=int, default=40)
    parser.add_argument('--min-score', type=float, default=0.52)
    parser.add_argument('--contact-sheets', action='store_true')
    parser.add_argument('--copy', action='store_true')
    args = parser.parse_args()
    source = Path(args.source)
    destination = Path(args.destination)
    if not source.is_dir():
        sys.exit(f'Source folder not found: {source}')
    prefixes = {m.group(1).upper() for p in source.iterdir()
                if p.suffix.lower() == '.arw'
                for m in [re.fullmatch(r'([A-Za-z]{3})\d+', p.stem)] if m}
    if args.prefix:
        prefix = args.prefix.upper()
        if not re.fullmatch('[A-Z]{3}', prefix):
            sys.exit('Prefix must be 3 Latin letters')
    elif len(prefixes) == 1:
        prefix = next(iter(prefixes))
    else:
        sys.exit(f'Cannot auto-detect unique prefix {sorted(prefixes)}; specify --prefix ABC')
    print('Camera prefix:', prefix)
    raws = index_files(source, '.ARW', prefix)
    jpegs = index_files(source, '.JPG', prefix)
    for n, p in index_files(source, '.JPEG', prefix).items():
        jpegs.setdefault(n, p)
    students = read_manifest(args.manifest)
    for name, numbers in students:
        missing = [n for n in numbers if n not in raws or n not in jpegs]
        if missing:
            sys.exit(f'Missing RAW/JPEG for {name}: {missing}')
    try:
        from insightface.app import FaceAnalysis
    except ImportError:
        sys.exit('Dependencies missing. Run: py -m pip install -r requirements.txt')
    print('Starting local face recognition. Model weights may download on first run.')
    model = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
    model.prepare(ctx_id=-1, det_size=(640, 640))
    cache = {}
    def inspect(n):
        if n not in cache:
            img = image_at(jpegs.get(n))
            if img is None or is_blank(img):
                cache[n] = {'faces': [], 'hash': None, 'blank': True}
            else:
                faces = model.get(img)
                cache[n] = {'faces': [f.normed_embedding for f in faces],
                            'hash': image_hash(img), 'blank': False}
        return cache[n]
    review = Path('fobox_review')
    if args.contact_sheets:
        review.mkdir(exist_ok=True)
    plan = []
    for name, required in students:
        references = []
        for n in required[:3]:
            faces = inspect(n)['faces']
            if len(faces) == 1:
                references.append(faces[0])
        if len(references) < 2:
            print('WARNING: insufficient unique portrait reference faces for', name)
        candidates = []
        nearby = sorted((set(raws) & set(jpegs)) - set(required))
        for n in nearby:
            if min(abs(n - a) for a in required) > args.radius:
                continue
            item = inspect(n)
            if item['blank'] or not item['faces'] or len(references) < 2:
                continue
            score = max(float(np.dot(ref, face)) for ref in references for face in item['faces'])
            if score >= args.min_score:
                candidates.append((n, score, item['hash']))
        selected = []
        hashes = [inspect(n)['hash'] for n in required if inspect(n)['hash'] is not None]
        for n, score, hash_value in sorted(candidates, key=lambda x: -x[1]):
            if all(np.count_nonzero(hash_value != old) >= 9 for old in hashes):
                selected.append((n, round(score, 3)))
                hashes.append(hash_value)
        filenames = {
            required[0]: f'{name}_{raws[required[0]].name}',
            required[1]: f'ЛР_{name}_{raws[required[1]].name}',
            required[2]: f'ЛС_{name}_{raws[required[2]].name}',
            required[3]: f'Друг1_{raws[required[3]].name}',
            required[4]: f'Друг2_{raws[required[4]].name}'
        }
        entries = [{'number': n, 'source': str(raws[n]),
                    'output': filenames.get(n, raws[n].name),
                    'mandatory': n in required}
                   for n in sorted(set(required + [n for n, _ in selected]))]
        plan.append({'student': name, 'entries': entries, 'additional_scores': selected})
        if args.contact_sheets:
            tiles = []
            for entry in entries:
                n = entry['number']
                img = image_at(jpegs[n])
                if img is None:
                    continue
                h, w = img.shape[:2]
                scale = min(190 / w, 230 / h)
                thumb = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
                tile = np.full((275, 210, 3), 245, dtype=np.uint8)
                x = (210 - thumb.shape[1]) // 2
                y = (240 - thumb.shape[0]) // 2
                tile[y:y + thumb.shape[0], x:x + thumb.shape[1]] = thumb
                cv2.putText(tile, f'{prefix}{n:05d}' + (' *' if entry['mandatory'] else ''),
                            (8, 262), cv2.FONT_HERSHEY_SIMPLEX, .52, (0, 0, 0), 1)
                tiles.append(tile)
            if tiles:
                columns = 5
                sheet = np.full((math.ceil(len(tiles) / columns) * 275, columns * 210, 3),
                                255, dtype=np.uint8)
                for i, tile in enumerate(tiles):
                    x, y = (i % columns) * 210, (i // columns) * 275
                    sheet[y:y + 275, x:x + 210] = tile
                safe = re.sub(r'[^\w -]', '_', name)
                cv2.imencode('.jpg', sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tofile(
                    str(review / (safe + '.jpg')))
        print(f'{name}: {len(entries)} RAW, {len(selected)} extra')
    Path('fobox_v2_plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                                         encoding='utf-8')
    print('Plan saved to fobox_v2_plan.json')
    if not args.copy:
        print('DRY RUN: inspect contact sheets before copying.')
        return
    if destination.resolve() == source.resolve() or source.resolve() in destination.resolve().parents:
        sys.exit('Destination must not be inside source')
    if destination.exists() and any(destination.iterdir()):
        sys.exit('Destination must be empty')
    if input('Type COPY to copy selected RAW files: ') != 'COPY':
        return
    for student in plan:
        folder = destination / student['student']
        folder.mkdir(parents=True, exist_ok=False)
        for entry in student['entries']:
            shutil.copy2(entry['source'], folder / entry['output'])
    print('RAW files copied to', destination)

if __name__ == '__main__':
    main()
