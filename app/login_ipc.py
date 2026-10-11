"""Encrypted login results shared with the windowed worker process."""
import base64
import json

from .cookies import protect
from .storage import read_json, write_json


def write_result(path, result):
    payload = protect(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    write_json(path, {'protected': base64.b64encode(payload).decode('ascii')})


def read_result(path):
    envelope = read_json(path, {})
    if not envelope.get('protected'):
        raise RuntimeError('登录窗口未返回结果，请检查 WebView2 组件后重试')
    return json.loads(protect(base64.b64decode(envelope['protected']), decrypt=True))


def run_worker(path, opener=None):
    try:
        if opener is None:
            from .login import LoginWindow
            opener = LoginWindow().open
        result = opener()
    except Exception as error:
        result = {'ok': False, 'reason': 'error', 'message': str(error)}
    write_result(path, result)
