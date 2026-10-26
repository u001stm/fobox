"""Минимальный клиент REST API Яндекс.Диска (только стандартная библиотека).

Нужен OAuth-токен с правами «Запись в любом месте на Диске» и «Чтение всего Диска».
Документация API: https://yandex.ru/dev/disk/rest/
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get('LUMIO_YADISK_API', 'https://cloud-api.yandex.net/v1/disk')


class DiskError(Exception):
    pass


class YaDisk:
    def __init__(self, token, api=None):
        if not token:
            raise DiskError('Нет токена Яндекс.Диска — добавьте его в Настройки → Яндекс.Диск')
        self.token, self.api = token.strip(), (api or globals()['API']).rstrip('/')

    def _req(self, method, url, data=None, headers=None, raw=False, ok=(200, 201, 202, 204)):
        h = {'Authorization': 'OAuth ' + self.token, 'Accept': 'application/json'}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
                return r.status, (body if raw else (json.loads(body) if body else {}))
        except urllib.error.HTTPError as e:
            try:
                info = json.loads(e.read() or b'{}')
            except ValueError:
                info = {}
            if e.code in ok:
                return e.code, info
            if e.code == 401:
                raise DiskError('Яндекс.Диск не принял токен — получите новый в Настройки → Яндекс.Диск')
            if e.code == 507:
                raise DiskError('На Яндекс.Диске закончилось место')
            raise DiskError(f'Яндекс.Диск: {info.get("message") or info.get("description") or e.reason} ({e.code})')
        except urllib.error.URLError as e:
            raise DiskError(f'Нет связи с Яндекс.Диском: {e.reason}')

    def _url(self, endpoint, **params):
        q = urllib.parse.urlencode(params) if params else ''
        return f'{self.api}{endpoint}' + ('?' + q if q else '')

    def info(self):
        _, j = self._req('GET', self._url(''))
        return {'user': (j.get('user') or {}).get('display_name') or (j.get('user') or {}).get('login', ''),
                'total': j.get('total_space'), 'used': j.get('used_space')}

    def mkdir(self, path):
        code, _ = self._req('PUT', self._url('/resources', path=path), ok=(201, 409))
        return code == 201

    def mkdirs(self, path):
        parts = [p for p in path.replace('disk:/', '').split('/') if p]
        cur = 'disk:'
        for p in parts:
            cur += '/' + p
            self.mkdir(cur)

    def exists(self, path):
        code, j = self._req('GET', self._url('/resources', path=path, fields='size,type,md5'), ok=(200, 404))
        return j if code == 200 else None

    def upload(self, local, remote, md5=None, force=False):
        """Upload one file. A file already on the Disk with the same size (and the same MD5, when known)
        is skipped. Returns ('uploaded' | 'skipped', md5 of the local file)."""
        size = os.path.getsize(local)
        have = self.exists(remote)
        if have and not force and have.get('size') == size:
            md5 = md5 or file_md5(local)
            if not have.get('md5') or have['md5'] == md5:
                return 'skipped', md5
        code, j = self._req('GET', self._url('/resources/upload', path=remote, overwrite='true' if have else 'false'), ok=(200, 409))
        if code == 409:
            return 'skipped', md5 or file_md5(local)
        last = None
        for attempt in range(3):                       # a dropped connection is retried
            h = hashlib.md5()
            try:
                with open(local, 'rb') as f:
                    req = urllib.request.Request(j['href'], data=_Hashing(f, h), method=j.get('method', 'PUT'),
                                                 headers={'Content-Length': str(size), 'Content-Type': 'application/octet-stream'})
                    with urllib.request.urlopen(req, timeout=600) as r:
                        if r.status not in (200, 201, 202):
                            raise DiskError(f'Загрузка не удалась ({r.status})')
                return 'uploaded', h.hexdigest()
            except urllib.error.HTTPError as e:
                last = DiskError(f'Загрузка {os.path.basename(local)} не удалась ({e.code})')
            except (urllib.error.URLError, OSError) as e:
                last = DiskError(f'Связь прервалась на {os.path.basename(local)}: {getattr(e, "reason", e)}')
            time.sleep(2 * (attempt + 1))
            code, j = self._req('GET', self._url('/resources/upload', path=remote, overwrite='true'))
        raise last

    def tree(self, path):
        """Every file under `path`: {relative path: {'size', 'md5'}} — used to verify an upload."""
        out, todo = {}, ['']
        while todo:
            rel = todo.pop()
            offset = 0
            while True:
                code, j = self._req('GET', self._url('/resources', path=path + ('/' + rel if rel else ''), limit=1000, offset=offset,
                                                     fields='_embedded.items.name,_embedded.items.type,_embedded.items.size,'
                                                            '_embedded.items.md5,_embedded.total'), ok=(200, 404))
                if code == 404:
                    break
                items = (j.get('_embedded') or {}).get('items') or []
                for it in items:
                    r = (rel + '/' if rel else '') + it['name']
                    if it.get('type') == 'dir':
                        todo.append(r)
                    else:
                        out[r] = {'size': it.get('size'), 'md5': it.get('md5')}
                offset += len(items)
                if not items or offset >= ((j.get('_embedded') or {}).get('total') or 0):
                    break
        return out

    def publish(self, path):
        """Public link to the folder. In a folder shared with you the owner may not allow links: then ''."""
        try:
            self._req('PUT', self._url('/resources/publish', path=path))
            _, j = self._req('GET', self._url('/resources', path=path, fields='public_url'))
            return j.get('public_url', '')
        except DiskError:
            return ''


class _Hashing:
    """File wrapper that feeds MD5 while the file is being sent."""
    def __init__(self, f, h):
        self.f, self.h = f, h

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        return b


def file_md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()
