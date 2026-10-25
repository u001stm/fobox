"""Минимальный клиент REST API Яндекс.Диска (только стандартная библиотека).

Нужен OAuth-токен с правами «Запись в любом месте на Диске» и «Чтение всего Диска».
Документация API: https://yandex.ru/dev/disk/rest/
"""
import json
import os
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
        code, j = self._req('GET', self._url('/resources', path=path, fields='size,type'), ok=(200, 404))
        return j if code == 200 else None

    def upload(self, local, remote):
        """Upload one file; an existing file of the same size is skipped. Returns 'uploaded' or 'skipped'."""
        size = os.path.getsize(local)
        have = self.exists(remote)
        if have and have.get('size') == size:
            return 'skipped'
        code, j = self._req('GET', self._url('/resources/upload', path=remote, overwrite='true' if have else 'false'), ok=(200, 409))
        if code == 409:
            return 'skipped'
        with open(local, 'rb') as f:
            req = urllib.request.Request(j['href'], data=f, method=j.get('method', 'PUT'),
                                         headers={'Content-Length': str(size), 'Content-Type': 'application/octet-stream'})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    if r.status not in (200, 201, 202):
                        raise DiskError(f'Загрузка не удалась ({r.status})')
            except urllib.error.HTTPError as e:
                raise DiskError(f'Загрузка {os.path.basename(local)} не удалась ({e.code})')
        return 'uploaded'

    def publish(self, path):
        self._req('PUT', self._url('/resources/publish', path=path))
        _, j = self._req('GET', self._url('/resources', path=path, fields='public_url'))
        return j.get('public_url', '')
