"""Local face analysis for LUMIO (no internet, no cloud).

Uses the InsightFace «buffalo_l» models through onnxruntime only — no insightface/opencv packages:
  det_10g.onnx   — face detector (SCRFD)            -> boxes + 5 key points
  w600k_r50.onnx — face recognition (ArcFace)       -> 512-d embedding (who is it)
  2d106det.onnx  — 106 face landmarks               -> are the eyes open
  genderage.onnx — gender / age estimate            -> girls / boys group photos
Models live in %USERPROFILE%\\.insightface\\models\\buffalo_l (the same place InsightFace uses).
"""
import io
import math
import os
import threading
import urllib.request
import zipfile
from pathlib import Path

try:
    import numpy as np
    import onnxruntime as ort
except ImportError:          # optional: LUMIO works without faces
    np = ort = None
try:
    from PIL import Image
except ImportError:
    Image = None

MODEL_URL = 'https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip'
NEEDED = ('det_10g.onnx', 'w600k_r50.onnx', '2d106det.onnx', 'genderage.onnx')
ARCFACE_DST = [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]]
# 106-point layout: the two eye contours
EYE_A = list(range(33, 43))
EYE_B = list(range(87, 97))


def model_dir():
    return Path(os.environ.get('LUMIO_FACE_MODELS') or Path.home() / '.insightface' / 'models' / 'buffalo_l')


def status():
    """What is missing for face analysis: packages, models — or nothing."""
    if np is None or ort is None:
        return {'ready': False, 'missing': 'packages', 'text': 'Не установлены onnxruntime и numpy'}
    d = model_dir()
    miss = [m for m in NEEDED if not (d / m).exists()]
    if miss:
        return {'ready': False, 'missing': 'models', 'text': 'Нет моделей лиц (buffalo_l, ~280 МБ)', 'dir': str(d)}
    return {'ready': True, 'text': 'Лица: готово', 'dir': str(d)}


def download_models(job=None):
    """Download and unpack buffalo_l (~280 MB) into the InsightFace model folder."""
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d.parent / 'buffalo_l.zip.part'
    with urllib.request.urlopen(MODEL_URL, timeout=120) as r, open(tmp, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        if job is not None:
            job.update(stage='Загрузка моделей лиц', total=total, done=0)
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            if job is not None:
                job['done'] += len(b)
    with zipfile.ZipFile(tmp) as z:
        for n in z.namelist():
            if n.endswith('.onnx'):
                (d / Path(n).name).write_bytes(z.read(n))
    tmp.unlink()


def _similarity(src, dst):
    """Least-squares similarity transform src->dst (2x3)."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    n = len(src)
    A = np.zeros((2 * n, 4))
    b = np.zeros(2 * n)
    A[0::2] = np.c_[src[:, 0], -src[:, 1], np.ones(n), np.zeros(n)]
    A[1::2] = np.c_[src[:, 1], src[:, 0], np.zeros(n), np.ones(n)]
    b[0::2], b[1::2] = dst[:, 0], dst[:, 1]
    a, c, tx, ty = np.linalg.lstsq(A, b, rcond=None)[0]
    return np.array([[a, -c, tx], [c, a, ty]])


def _warp(img, M, size):
    """Crop `img` (PIL) with forward transform M (image -> crop) into size x size, as float RGB array."""
    full = np.vstack([M, [0, 0, 1]])
    inv = np.linalg.inv(full)[:2]
    out = img.transform((size, size), Image.AFFINE, inv.flatten().tolist(), resample=Image.BILINEAR)
    return np.asarray(out, dtype=np.float32)


class FaceEngine:
    _lock = threading.Lock()

    def __init__(self, threads=None):
        st = status()
        if not st['ready']:
            raise RuntimeError(st['text'])
        d = model_dir()
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or min(2, max(1, (os.cpu_count() or 2) - 1))
        so.log_severity_level = 3
        mk = lambda n: ort.InferenceSession(str(d / n), so, providers=['CPUExecutionProvider'])
        self.det, self.rec, self.lmk, self.ga = mk('det_10g.onnx'), mk('w600k_r50.onnx'), mk('2d106det.onnx'), mk('genderage.onnx')
        self.det_in = self.det.get_inputs()[0].name
        self.det_out = [o.name for o in self.det.get_outputs()]

    # -------------------------------------------------- detection (SCRFD)
    def detect(self, img, size=640, thresh=0.5):
        w, h = img.size
        scale = size / max(w, h)
        nw, nh = int(w * scale), int(h * scale)
        canvas = np.zeros((size, size, 3), np.float32)
        canvas[:nh, :nw] = np.asarray(img.resize((nw, nh), Image.BILINEAR), dtype=np.float32)
        blob = ((canvas - 127.5) / 128.0).transpose(2, 0, 1)[None]
        outs = self.det.run(self.det_out, {self.det_in: blob})
        boxes, scores, kpss = [], [], []
        for i, stride in enumerate((8, 16, 32)):
            sc, bb, kp = outs[i][:, 0], outs[i + 3] * stride, outs[i + 6] * stride
            fh, fw = size // stride, size // stride
            cy, cx = np.mgrid[:fh, :fw]
            centers = np.stack([cx, cy], -1).reshape(-1, 2).astype(np.float32) * stride
            centers = np.repeat(centers, 2, axis=0)
            keep = np.where(sc >= thresh)[0]
            if not len(keep):
                continue
            c, b_, k_ = centers[keep], bb[keep], kp[keep]
            boxes.append(np.c_[c[:, 0] - b_[:, 0], c[:, 1] - b_[:, 1], c[:, 0] + b_[:, 2], c[:, 1] + b_[:, 3]])
            scores.append(sc[keep])
            kpss.append((np.repeat(c, 5, axis=0).reshape(-1, 5, 2) + k_.reshape(-1, 5, 2)))
        if not boxes:
            return []
        boxes, scores, kpss = np.vstack(boxes) / scale, np.concatenate(scores), np.vstack(kpss) / scale
        order = scores.argsort()[::-1]
        keep = []
        while len(order):
            i = order[0]
            keep.append(i)
            xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0]); yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
            xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2]); yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
            inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
            area = lambda j: (boxes[j, 2] - boxes[j, 0]) * (boxes[j, 3] - boxes[j, 1])
            iou = inter / (area(i) + area(order[1:]) - inter + 1e-9)
            order = order[1:][iou <= 0.4]
        return [{'box': boxes[i].tolist(), 'score': float(scores[i]), 'kps': kpss[i].tolist()} for i in keep]

    # -------------------------------------------------- per face
    def embed(self, img, kps):
        M = _similarity(kps, ARCFACE_DST)
        x = (_warp(img, M, 112) - 127.5) / 127.5
        e = self.rec.run(None, {self.rec.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0][0]
        return e / (np.linalg.norm(e) + 1e-9)

    def _crop(self, img, box, size):
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        s = size / (max(x2 - x1, y2 - y1) * 1.5)
        M = np.array([[s, 0, size / 2 - cx * s], [0, s, size / 2 - cy * s]])
        return _warp(img, M, size), M

    def eyes(self, img, box):
        """Eye openness: eye height / eye width over both eyes (≈0.25–0.4 open, <0.12 closed)."""
        x, M = self._crop(img, box, 192)
        p = self.lmk.run(None, {self.lmk.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0][0].reshape(-1, 2)
        p = (p + 1) * 96
        ratio = []
        for idx in (EYE_A, EYE_B):
            e = p[idx]
            wdt = e[:, 0].max() - e[:, 0].min()
            hgt = e[:, 1].max() - e[:, 1].min()
            ratio.append(hgt / (wdt + 1e-6))
        return float(min(ratio)), float(max(ratio))

    def gender_age(self, img, box):
        x, _ = self._crop(img, box, 96)
        g = self.ga.run(None, {self.ga.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0][0]
        return ('m' if g[1] > g[0] else 'f'), int(round(g[2] * 100))

    def analyze(self, img):
        """All faces of a frame, biggest first: box, size (share of the frame height), embedding, eyes, gender, age."""
        img = img.convert('RGB')
        with self._lock:
            faces = self.detect(img)
            out = []
            H = img.size[1]
            for f in faces:
                x1, y1, x2, y2 = f['box']
                if min(x2 - x1, y2 - y1) < 14:
                    continue
                emb = self.embed(img, f['kps'])
                lo, hi = self.eyes(img, f['box'])
                g, a = self.gender_age(img, f['box'])
                out.append({'box': [round(v, 1) for v in f['box']], 'score': round(f['score'], 3), 'iw': img.size[0], 'ih': H,
                            'h': round((y2 - y1) / H, 4), 'emb': emb.astype(np.float32),
                            'eye': round(lo, 3), 'eye2': round(hi, 3), 'g': g, 'age': a})
        out.sort(key=lambda f: -f['h'])
        return out


def similar(a, b):
    return float(np.dot(a, b))
