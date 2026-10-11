"""Atomic settings and queue persistence. Never persist signed media URLs."""
import json
import os
import threading
from pathlib import Path

from . import config


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


class Settings:
    def __init__(self):
        self.path = Path(config.data_dir()) / 'settings.json'
        self.values = {'directory': config.default_download_dir(),
                       'quality': config.QUALITY_OPTIONS[1], 'workers': 2, 'last_url': ''}
        saved = read_json(self.path, {})
        if isinstance(saved, dict):
            self.values.update(saved)
        if self.values['quality'] not in config.QUALITY_OPTIONS:
            self.values['quality'] = config.QUALITY_OPTIONS[1]
        try:
            self.values['workers'] = max(1, min(4, int(self.values['workers'])))
        except (ValueError, TypeError):
            self.values['workers'] = 2

    def save(self, **values):
        self.values.update(values)
        write_json(self.path, self.values)


class QueueStore:
    def __init__(self, path=None):
        self.path = Path(path or Path(config.data_dir()) / 'queue.json')
        self.lock = threading.RLock()
        # File associations survive clearing the visible queue, so re-adding a
        # resource can still skip its completed file or resume its partial file.
        self.files_path = self.path.with_name(self.path.stem + '-files.json')
        files = read_json(self.files_path, {})
        self.files = {key: value for key, value in files.items() if isinstance(value, str)} if isinstance(files, dict) else {}
        data = read_json(self.path, [])
        self.tasks = {t['id']: t for t in data if isinstance(t, dict) and 'id' in t} if isinstance(data, list) else {}
        self.remove([key for key, task in self.tasks.items() if task.get('status') == 'removing'])
        for task in self.tasks.values():
            if task.get('status') in ('waiting', 'resolving', 'downloading', 'merging'):
                task.update(status='paused', error='上次任务中断，可继续下载', speed=0, eta=None)

    def save(self):
        with self.lock:
            write_json(self.path, list(self.tasks.values()))

    def update(self, task_id, **changes):
        with self.lock:
            self.tasks[task_id].update(changes)
            return dict(self.tasks[task_id])

    def remove(self, ids):
        """Remove records only. Commit file associations before queue deletion."""
        with self.lock:
            keys = set(ids) & self.tasks.keys()
            if not keys:
                return []
            files = dict(self.files)
            for key in keys:
                if self.tasks[key].get('path'):
                    files[key] = self.tasks[key]['path']
            remaining = {key: task for key, task in self.tasks.items() if key not in keys}
            write_json(self.files_path, files)
            self.files = files
            write_json(self.path, list(remaining.values()))
            self.tasks = remaining
            return list(keys)

    def snapshot(self):
        with self.lock:
            return [dict(t) for t in self.tasks.values()]
