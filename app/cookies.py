"""Validated identity, cookie attributes, Windows per-user encryption."""
import base64
import ctypes
import json
import os
import threading
import time
from pathlib import Path
import requests
from . import config
from .storage import read_json, write_json

LOGIN_COOKIES = ('NTES_YSESS', 'NTES_SESS', 'P_INFO', 'P_OINFO', 'S_INFO')


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('登录信息安全存储仅支持 Windows')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('Windows 无法保护登录信息')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(target.data)


class CookieManager:
    def __init__(self, path=None):
        self.path = str(path or Path(config.data_dir()) / 'account.json')
        self.lock = threading.RLock()
        self.records, self.user, self.verified = [], {}, False
        data = read_json(self.path, {})
        if data.get('protected'):
            try:
                payload = json.loads(protect(base64.b64decode(data['protected']), True))
                self.records = payload.get('cookies', [])
                self.user = payload.get('user', {})
            except Exception:
                pass

    @property
    def cookies(self):
        return {c['name']: c['value'] for c in self.records if not c.get('expires') or c['expires'] > time.time()}

    def save(self, cookie_data, user=None):
        if isinstance(cookie_data, dict):
            cookie_data = [{'name': k, 'value': v, 'domain': '.icourse163.org', 'path': '/'} for k, v in cookie_data.items()]
        with self.lock:
            records = [dict(c) for c in cookie_data if c.get('name') and c.get('value')
                       and (str(c.get('domain', '')).lstrip('.') == 'icourse163.org'
                            or str(c.get('domain', '')).lstrip('.').endswith('.icourse163.org'))]
            payload = {'cookies': records, 'user': user or {}}
            encrypted = protect(json.dumps(payload).encode('utf-8'))
            write_json(self.path, {'protected': base64.b64encode(encrypted).decode('ascii')})
            self.records, self.user, self.verified = records, user or {}, bool((user or {}).get('id'))

    def logged_in(self):
        return self.verified and bool(self.user.get('id'))

    def build_session(self):
        s = requests.Session()
        s.headers.update({'User-Agent': config.USER_AGENT, 'Referer': config.REFERER})
        with self.lock:
            for c in self.records:
                if c.get('expires') and c['expires'] <= time.time():
                    continue
                s.cookies.set(c['name'], c['value'], domain=c.get('domain') or '.icourse163.org',
                              path=c.get('path') or '/', secure=bool(c.get('secure', True)), expires=c.get('expires'))
        return s

    def clear(self):
        with self.lock:
            Path(self.path).unlink(missing_ok=True)
            self.records, self.user, self.verified = [], {}, False

    @staticmethod
    def parse_cookie_header(cookie_str):
        pairs = [p.strip().split('=', 1) for p in (cookie_str or '').split(';') if '=' in p]
        return {k.strip(): v.strip() for k, v in pairs if k.strip()}

    def csrf_key(self):
        return self.cookies.get('NTESSTUDYSI', '')
