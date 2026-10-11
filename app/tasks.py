"""Persistent task queue. GUI views never own download selection or paths."""
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from . import config
from .downloader import DownloadManager, InterruptedError_, completed
from .icourse_api import CourseClient, PaidCourseError, LoginExpired, ResourceUnavailable
from .models import Unit
from .storage import QueueStore
from .transfer_state import path_key, read_record, relocate_complete

ACTIVE = {'waiting', 'resolving', 'downloading', 'merging'}
FINISHED = {'done', 'skipped'}
STATUS_TEXT = {'waiting': '等待中', 'resolving': '获取地址', 'downloading': '下载中',
               'merging': '合并中', 'done': '已完成', 'skipped': '已存在',
               'failed': '失败', 'paused': '已暂停', 'unavailable': '不可下载', 'removing': '正在移除'}


def make_task(course, unit, directory, quality):
    key = '%s:%s:%s:%s:%s' % (course.course_id, course.term_id, unit.unit_id,
                               quality if unit.is_video else 'doc', os.path.normcase(os.path.abspath(directory)))
    return {'id': hashlib.sha256(key.encode('utf-8')).hexdigest()[:24],
            'course_id': course.course_id, 'term_id': course.term_id, 'course': course.name,
            'unit': asdict(unit), 'name': unit.name, 'kind': 'video' if unit.is_video else 'doc',
            'directory': os.path.abspath(directory), 'quality': quality, 'actual_quality': '',
            'path': '', 'status': 'waiting', 'percent': 0, 'received': 0, 'total': 0,
            'speed': 0, 'eta': None, 'error': '', 'created': time.time()}


def output_path(task, extension, legacy=False):
    u = task['unit']
    clean = lambda value: config.sanitize_filename(value)[:36].rstrip(' .') or '未命名'
    directory = Path(task['directory']) / ('%s_%s' % (clean(task['course']), task['term_id']))
    directory /= '%02d %s' % (u.get('chapter_index', 0), clean(u['chapter_name']))
    directory /= '%02d %s' % (u.get('lesson_index', 0), clean(u['lesson_name']))
    name = '%02d %s' % (u.get('unit_index', 0), clean(u['name']))
    if legacy:
        quality = '-Q%d' % config.QUALITY_RANK.get(task['quality'], 2) if task['kind'] == 'video' else ''
        name += ' [%s]%s' % (u['unit_id'], quality)
    return str(directory / (name + extension))


class TaskEngine:
    def __init__(self, client_factory, event_cb, store=None):
        self.store = store or QueueStore()
        self.client_factory, self.event = client_factory, event_cb
        self.controls, self.thread = {}, None
        self._inflight = set()
        self.slots, self.merge_lock = threading.BoundedSemaphore(8), threading.Lock()
        self._persisted = 0
        self._migration_backup = False
        self.organize_downloads()

    def _backup_queue(self):
        if not self._migration_backup and self.store.path.is_file():
            directory = Path(config.data_dir())/'backups'
            directory.mkdir(parents=True, exist_ok=True)
            backup = directory / ('queue-before-clean-names-%s.json' % time.time_ns())
            shutil.copy2(self.store.path, backup)
        self._migration_backup = True

    def _free_path(self, task, extension):
        """Called while holding the queue lock; queued tasks reserve names too."""
        base = Path(output_path(task, extension))
        reserved = {path_key(t['path']) for t in self.store.tasks.values()
                    if t['id'] != task['id'] and t.get('path')}
        reserved.update(path_key(path) for key, path in self.store.files.items() if key != task['id'])
        index, candidate = 1, base
        while (path_key(candidate) in reserved or candidate.exists()
               or Path(str(candidate)+'.part').exists() or Path(str(candidate)+'.parts').exists()):
            index += 1
            candidate = base.with_name('%s (%d)%s' % (base.stem, index, base.suffix))
        return str(candidate)

    def _reserve_path(self, task_id, extension):
        with self.store.lock:
            task = self.store.tasks[task_id]
            path = task.get('path')
            # Keep the old path of a partially downloaded resource until it is
            # complete; its .part / .parts cache remains usable after upgrading.
            if (path and Path(path).suffix == extension
                    and Path(path).resolve().parent == Path(output_path(task, extension)).resolve().parent):
                return path
            path = self._free_path(task, extension)
            task['path'] = path
            self.store.save()
            return path

    def _organize_one(self, task_id):
        with self.store.lock:
            task = self.store.tasks[task_id]
            path = task.get('path')
            if not path or Path(path).suffix.lower() not in ('.mp4', '.flv', '.pdf', '.ppt', '.pptx', '.doc', '.docx'):
                return path
            extension = Path(path).suffix
            legacy = output_path(task, extension, legacy=True)
            base = Path(output_path(task, extension))
            if path_key(path) != path_key(legacy):
                completed(path)  # Import any valid old sidecar for a recorded file.
                return path
            if not base.resolve().is_relative_to(Path(task['directory']).resolve()):
                raise ValueError('文件不在此任务的课程目录内，保留原文件')
            if not Path(path).exists():
                # Recover if a previous process ended after renaming the file
                # but before saving the new path to the task queue.
                for candidate in base.parent.glob('*'+extension):
                    info = read_record(candidate, 'complete')
                    if (info.get('previous_path') == path_key(path) and info.get('task_id') == task_id
                            and completed(candidate)):
                        self._backup_queue()
                        task['path'] = str(candidate)
                        self.store.save()
                        return str(candidate)
                return path
            if not completed(path):
                return path
            self._backup_queue()
            target = self._free_path(task, extension)
            relocate_complete(path, target, task_id)
            task['path'] = target
            self.store.save()
            return target

    def organize_downloads(self):
        count, errors = 0, []
        for task in self.store.snapshot():
            try:
                if self._organize_one(task['id']) != task.get('path'):
                    count += 1
            except (OSError, ValueError, KeyError) as error:
                errors.append({'id': task['id'], 'error': str(error)})
                self.event(('log', '暂未整理 %s：%s；下次启动会重试' % (task.get('name', ''), error)))
        if count:
            self.event(('log', '已整理 %d 个旧下载文件，完成记录已移至软件数据目录' % count))
        self.organization_result = {'renamed_or_recovered': count, 'errors': errors}
        return self.organization_result

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def add(self, course, units, directory, quality):
        ids = []
        with self.store.lock:
            for unit in units:
                task = make_task(course, unit, directory, quality)
                old = self.store.tasks.get(task['id'])
                if old and old['status'] == 'removing':
                    continue
                if old and old['status'] in FINISHED and completed(old.get('path', '')):
                    continue
                if not old:
                    task['path'] = self.store.files.get(task['id'], '')
                    self.store.tasks[task['id']] = task
                elif old['status'] not in ACTIVE:
                    old.update(status='waiting', error='')
                ids.append(task['id'])
            self.store.save()
        return ids

    def update(self, task_id, **values):
        with self.store.lock:
            current = self.store.tasks.get(task_id)
            if not current:
                return
            if current['status'] == 'removing':
                values.update(status='removing', speed=0, eta=None)
            task = self.store.update(task_id, **values)
            now = time.monotonic()
            if 'status' in values or now-self._persisted > 2:
                self.store.save()
                self._persisted = now
        self.event(('task', task))

    def start(self, ids=None, workers=2):
        if self.running:
            return False
        snapshots = self.store.snapshot()
        ids = ids if ids is not None else [t['id'] for t in snapshots if t['status'] in {'waiting', 'paused', 'failed'}]
        tasks = {t['id']: t for t in snapshots}
        ids = [key for key in dict.fromkeys(ids) if key in tasks and tasks[key]['status'] != 'removing']
        if not ids:
            return False
        self.controls = {key: threading.Event() for key in ids}
        for key in ids:
            self.update(key, status='waiting', error='', speed=0, eta=None)
        # Alternate documents and videos so a large video never blocks all handouts.
        docs = [key for key in ids if tasks[key]['kind'] == 'doc']
        videos = [key for key in ids if tasks[key]['kind'] == 'video']
        ordered = []
        while docs or videos:
            if videos:
                ordered.append(videos.pop(0))
            if docs:
                ordered.append(docs.pop(0))
        with self.store.lock:
            scheduled = [(key, self.store.tasks[key]) for key in ordered if key in self.store.tasks]
        def work():
            try:
                with ThreadPoolExecutor(max_workers=max(1, min(4, workers))) as pool:
                    list(pool.map(lambda item: self._process(*item), scheduled))
            except Exception as error:
                self.event(('error', '任务队列异常：' + str(error)))
            finally:
                self.store.save()
                self.event(('queue_done',))
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True

    def pause(self, ids=None):
        with self.store.lock:
            keys = ids if ids is not None else [t['id'] for t in self.store.snapshot() if t['status'] in ACTIVE]
            for key in keys:
                task = self.store.tasks.get(key)
                if not task or task['status'] == 'removing':
                    continue
                if self.running and key in self.controls:
                    self.controls[key].set()
                elif task['status'] in ACTIVE:
                    self.update(key, status='paused', speed=0, eta=None)

    def remove(self, ids=None):
        """Stop selected workers before forgetting records; never delete media."""
        with self.store.lock:
            keys = [key for key in dict.fromkeys(self.store.tasks if ids is None else ids)
                    if key in self.store.tasks and self.store.tasks[key]['status'] != 'removing']
            active = [key for key in keys if key in self._inflight]
            for key in active:
                self.update(key, status='removing', speed=0, eta=None, error='正在停止下载并移除记录，文件和进度会保留')
                self.controls[key].set()
            removed = self.store.remove([key for key in keys if key not in self._inflight])
            for key in removed:
                if key in self.controls:
                    self.controls[key].set()
            if removed:
                self.event(('removed', removed))
            return len(keys)

    def _finish_processing(self, task_id):
        with self.store.lock:
            try:
                task = self.store.tasks.get(task_id)
                if task and task['status'] == 'removing':
                    removed = self.store.remove([task_id])
                    self.event(('removed', removed))
            except OSError as error:
                self.store.tasks[task_id].update(status='paused', error='移除记录失败：' + str(error))
                self.event(('task', dict(self.store.tasks[task_id])))
                self.event(('error', '无法保存移除结果，请稍后重试：' + str(error)))
            finally:
                self._inflight.discard(task_id)

    def _process(self, task_id, expected_task=None):
        with self.store.lock:
            if (task_id not in self.store.tasks
                    or (expected_task is not None and self.store.tasks[task_id] is not expected_task)):
                return
            self._inflight.add(task_id)
            task = dict(self.store.tasks[task_id])
            stop = self.controls[task_id]
        client = dm = None
        started = time.monotonic()
        try:
            unit = Unit(**task['unit'])
            if stop.is_set():
                raise InterruptedError_()
            if task.get('path') and completed(task['path']):
                self._organize_one(task_id)
                self.update(task_id, status='skipped', percent=100, error='')
                return
            self.update(task_id, status='resolving')
            client = self.client_factory()
            for attempt in range(3):
                if stop.is_set():
                    raise InterruptedError_()
                try:
                    if unit.is_video:
                        url, fmt, actual, duration = client.get_video_urls(unit, task['quality'])
                        extension = '.flv' if fmt == 'flv' else '.mp4'
                    else:
                        url, filename = client.resolve_doc(unit)
                        extension = Path(filename).suffix.lower()
                        extension = extension if extension in ('.pdf', '.ppt', '.pptx', '.doc', '.docx') else '.pdf'
                        fmt, actual, duration = '', '', 0
                    break
                except (PaidCourseError, LoginExpired, ResourceUnavailable):
                    raise
                except Exception:
                    if attempt == 2:
                        raise
                    if stop.wait(attempt+1):
                        raise InterruptedError_()
            if stop.is_set():
                raise InterruptedError_()
            path = self._reserve_path(task_id, extension)
            self.update(task_id, path=path, actual_quality=actual, duration=duration,
                        resolve_seconds=time.monotonic()-started)
            dm = DownloadManager(client.session,
                                 lambda message: self.event(('log', task['name']+'：'+message)), lambda *a: None,
                                 stop_event=stop, request_slots=self.slots, merge_lock=self.merge_lock,
                                 stats_cb=lambda **kw: self.update(task_id, **kw),
                                 state_cb=lambda state: self.update(task_id, status=state))
            callback = lambda p: self.update(task_id, percent=p)
            transfer = time.monotonic()
            if unit.is_video:
                ok = dm._download_video(unit, url, fmt, task['quality'], path, config.find_ffmpeg(), callback)
                quality = self._probe_quality(path)
                if quality:
                    self.update(task_id, actual_quality=quality)
            else:
                ok = dm._download_doc(unit, url, path, callback)
            path = self._organize_one(task_id) or path
            self.update(task_id, status='done' if ok else 'skipped', percent=100,
                        received=Path(path).stat().st_size, total=Path(path).stat().st_size,
                        speed=0, eta=0, error='', transfer_seconds=time.monotonic()-transfer)
        except InterruptedError_:
            self.update(task_id, status='paused', speed=0, eta=None, error='进度已保留，点击继续即可恢复')
        except (PaidCourseError, ResourceUnavailable) as error:
            self.update(task_id, status='unavailable', speed=0, eta=None, error=str(error))
        except Exception as error:
            message = re.sub(r'https?://\S+', '[地址已隐藏]', str(error))
            self.update(task_id, status='failed', speed=0, eta=None, error=message)
        finally:
            try:
                if dm:
                    dm.close()
                if client:
                    client.session.close()
            finally:
                self._finish_processing(task_id)

    @staticmethod
    def _probe_quality(path):
        ffmpeg = config.find_ffmpeg()
        if not ffmpeg:
            return ''
        try:
            result = subprocess.run([ffmpeg, '-hide_banner', '-i', path], capture_output=True,
                                    encoding='utf-8', errors='replace', timeout=10,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            for line in result.stderr.splitlines():
                if 'Video:' in line:
                    match = re.search(r'\b(\d{2,5})x(\d{2,5})\b', line)
                    if match:
                        return '%sP · %s×%s' % (match[2], match[1], match[2])
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ''
