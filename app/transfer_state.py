"""Keep transfer metadata in application data, not next to finished courses."""
import hashlib
import os
import threading
from pathlib import Path

from . import config
from .storage import read_json, write_json

_lock = threading.RLock()


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def record_path(path, kind, local=False):
    if kind not in ('part', 'complete'):
        raise ValueError('Unknown transfer record type')
    if local:
        return Path(str(path) + '.' + kind + '.json')
    key = hashlib.sha256(path_key(path).encode('utf-8')).hexdigest()
    return Path(config.data_dir())/'transfers'/key[:2]/(key+'.'+kind+'.json')


def _valid(info, kind):
    return (isinstance(info, dict) and isinstance(info.get('source'), str)
            and info['source'].startswith(('http://', 'https://'))
            and (kind == 'part' or (isinstance(info.get('size'), int) and info['size'] > 0)))


def read_record(path, kind, local=False):
    with _lock:
        target = record_path(path, kind, local)
        info = read_json(target, {})
        if _valid(info, kind):
            if not local:
                legacy = record_path(path, kind, local=True)
                old = read_json(legacy, {})
                if _valid(old, kind) and all(info.get(key) == value for key, value in old.items()):
                    legacy.unlink(missing_ok=True)
            return info
        if local:
            return {}
        # Import only recognizable v2 records whose companion file is present.
        # Preserve unrelated or malformed JSON files rather than deleting them.
        legacy = record_path(path, kind, local=True)
        info = read_json(legacy, {})
        if not _valid(info, kind):
            return {}
        companion = Path(path if kind == 'complete' else str(path)+'.part')
        try:
            stat = companion.stat()
        except OSError:
            return {}
        if kind == 'complete':
            if stat.st_size != info['size']:
                return {}
            info['mtime_ns'] = stat.st_mtime_ns
        write_json(target, info)
        legacy.unlink()
        return info


def write_record(path, kind, info, local=False):
    with _lock:
        write_json(record_path(path, kind, local), info)


def remove_record(path, kind, local=False):
    with _lock:
        record_path(path, kind, local).unlink(missing_ok=True)


def mark_complete(path, source, local=False):
    stat = Path(path).stat()
    write_record(path, 'complete', {'source': source, 'size': stat.st_size,
                                   'mtime_ns': stat.st_mtime_ns}, local)


def relocate_complete(old, new, task_id):
    """Rename a known finished file without overwriting any existing file (Windows)."""
    old, new = Path(old), Path(new)
    if old.resolve().parent != new.resolve().parent:
        raise ValueError('整理只能在原小节目录内改名')
    if new.exists():
        raise FileExistsError(str(new))
    info = read_record(old, 'complete')
    stat = old.stat()
    if not info or info['size'] != stat.st_size or info.get('mtime_ns', stat.st_mtime_ns) != stat.st_mtime_ns:
        raise ValueError('文件完整性记录不匹配，保留原文件')
    # Write the destination record first. If a process stops after the rename,
    # previous_path and task_id let the queue recover its new path on next start.
    write_record(new, 'complete', dict(info, previous_path=path_key(old), task_id=task_id))
    try:
        os.rename(old, new)
    except Exception:
        remove_record(new, 'complete')
        raise
    remove_record(old, 'complete')
