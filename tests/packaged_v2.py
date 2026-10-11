"""Smoke-test the actual windowed executable without an account or network."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.login_ipc import read_result
from app.config import APP_VERSION

folder = ROOT/'_test_tmp'/'packaged-final'
folder.mkdir(parents=True, exist_ok=True)
program = ROOT/'dist'/'MOOCDownloader'/'MOOCDownloader.exe'
environment = dict(os.environ, MOOC_DATA_DIR=str(folder/'user-data'))
results = {}
for name, flag in [('startup', '--self-test'), ('webview', '--webview-self-test'),
                   ('login_ipc', '--login-ipc-self-test')]:
    output = folder/(name+'.json')
    output.unlink(missing_ok=True)
    subprocess.run([str(program), flag, str(output)], check=True, timeout=60,
                   env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    if name == 'login_ipc':
        value = read_result(output)
        assert value['ok'] and value['user']['nickName'] == '通信自检'
        assert value['cookies']['test'] == 'dummy-ipc-secret'
        assert 'dummy-ipc-secret' not in output.read_text(encoding='utf-8')
        results[name] = {'encrypted_roundtrip': True, 'windowed_process': True}
    else:
        value = json.loads(output.read_text(encoding='utf-8'))
        if name == 'startup':
            assert value['version'] == APP_VERSION and value['ffmpeg']
            assert value['pages'] == ['course', 'downloads', 'settings']
            assert value['queue_actions'] == ['移除所选任务', '清空下载记录']
        else:
            assert value.get('webview2'), value
        results[name] = value
(folder/'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(results, ensure_ascii=False, indent=2))
