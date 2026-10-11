"""Resumable transfers, atomic completion, cancellable HLS remuxing."""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
import m3u8
import requests
from . import config
from .icourse_api import PaidCourseError
from .transfer_state import read_record, write_record, remove_record, mark_complete

RETRY_TIMES = 3
HLS_WORKERS = 4


class InterruptedError_(Exception):
    pass


def identity(url):
    p = urlsplit(url)
    transient = {'ak', 'token', 'signature', 'expires', 'auth_key', 'wssecret', 'wstime'}
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in transient))
    return urlunsplit((p.scheme, p.netloc, p.path, query, ''))


def completed(path, source=None, local=False):
    if not path:
        return False
    info = read_record(path, 'complete', local)
    try:
        stat = Path(path).stat()
        return (info.get('size', 0) > 0 and stat.st_size == info['size']
                and info.get('mtime_ns', stat.st_mtime_ns) == stat.st_mtime_ns
                and (source is None or info.get('source') == identity(source)))
    except OSError:
        return False


class DownloadManager:
    STATUS_WAITING, STATUS_DOWNLOADING, STATUS_DONE = 'waiting', 'downloading', 'done'
    STATUS_SKIPPED, STATUS_FAILED, STATUS_STOPPED = 'skipped', 'failed', 'paused'

    def __init__(self, session, log_cb, progress_cb, item_cb=None, *, stop_event=None,
                 request_slots=None, merge_lock=None, stats_cb=None, state_cb=None, local_metadata=False):
        self.session = session or requests.Session()
        self.log, self.progress = log_cb, progress_cb
        self.item_cb = item_cb or (lambda *a: None)
        self._stop = stop_event or threading.Event()
        self._slots = request_slots or threading.BoundedSemaphore(8)
        self._merge_lock = merge_lock or threading.Lock()
        self.stats = stats_cb or (lambda **kw: None)
        self.state = state_cb or (lambda status: None)
        self._local = threading.local()
        self._sessions = []
        self.local_metadata = local_metadata

    def stop(self):
        self._stop.set()

    def check_stop(self):
        if self._stop.is_set():
            raise InterruptedError_('已暂停，可继续下载')

    def _acquire(self, semaphore):
        while not semaphore.acquire(timeout=.1):
            self.check_stop()
        try:
            self.check_stop()
        except Exception:
            semaphore.release()
            raise

    def _session(self):
        if not hasattr(self._local, 'session'):
            s = requests.Session()
            s.headers.update(self.session.headers)
            s.cookies.update(self.session.cookies)
            s.proxies.update(self.session.proxies)
            s.trust_env = self.session.trust_env
            self._local.session = s
            self._sessions.append(s)
        return self._local.session

    def close(self):
        for session in self._sessions:
            session.close()

    def run(self, tasks, ffmpeg_path, total=None):
        iterator, done = iter(tasks), 0
        total = total if total is not None else len(tasks) if hasattr(tasks, '__len__') else 0
        while not self._stop.is_set():
            try:
                task = next(iterator)
            except StopIteration:
                break
            unit, kind, url, fmt, quality, path = task
            name, uid = unit.name, getattr(unit, 'unit_id', None)
            callback = lambda pct: self.item_cb(name, uid, kind, 'downloading', pct)
            try:
                self.item_cb(name, uid, kind, 'downloading', 0)
                if kind == 'doc':
                    ok = self._download_doc(unit, url, path, callback)
                else:
                    ok = self._download_video(unit, url, fmt, quality, path, ffmpeg_path, callback)
                self.item_cb(name, uid, kind, 'done' if ok else 'skipped', 100)
            except InterruptedError_:
                self.item_cb(name, uid, kind, 'paused', getattr(self, '_stop_pct', 0))
                break
            except Exception as error:
                self.item_cb(name, uid, kind, 'failed', 0)
                self.log(str(error))
            done += 1
            self.progress(done, total or done, name)

    def _download_video(self, unit, url, fmt, quality, out_path, ffmpeg_path, progress_cb=None):
        if completed(out_path, url, self.local_metadata):
            return False
        self.state('downloading')
        if fmt == 'hls':
            self._download_hls(url, out_path, ffmpeg_path, progress_cb, quality)
        else:
            self._download_direct(url, out_path, unit.name, progress_cb)
        return True

    def _download_doc(self, unit, url, out_path, progress_cb=None):
        if completed(out_path, url, self.local_metadata):
            return False
        self.state('downloading')
        self._download_direct(url, out_path, unit.name, progress_cb)
        return True

    def _download_direct(self, url, path, label=None, progress_cb=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if completed(path, url, self.local_metadata):
            if progress_cb:
                progress_cb(100)
            return
        partial = Path(str(path) + '.part')
        source = identity(url)
        for attempt in range(RETRY_TIMES):
            self.check_stop()
            info = read_record(path, 'part', self.local_metadata)
            size = partial.stat().st_size if partial.exists() and info.get('source') == source else 0
            headers = {'Referer': config.REFERER, 'Accept-Encoding': 'identity'}
            if size:
                headers['Range'] = 'bytes=%d-' % size
                if info.get('validator'):
                    headers['If-Range'] = info['validator']
            self._acquire(self._slots)
            response = None
            try:
                response = self._session().get(url, headers=headers, stream=True, timeout=(10, 15))
                status = response.status_code
                if status == 416:
                    match = re.fullmatch(r'bytes \*/(\d+)', response.headers.get('Content-Range', ''))
                    valid = not info.get('validator') or info['validator'] in (response.headers.get('ETag'), response.headers.get('Last-Modified'))
                    if match and size > 0 and size == int(match[1]) and valid:
                        self._finish(partial, path, source, size)
                        remove_record(path, 'part', self.local_metadata)
                        return
                    partial.unlink(missing_ok=True)
                    remove_record(path, 'part', self.local_metadata)
                    raise RuntimeError('续传范围已变化，重新获取文件')
                if status not in (200, 206):
                    raise RuntimeError('下载请求失败（HTTP %d）' % status)
                if 'text/html' in response.headers.get('Content-Type', '').lower():
                    raise RuntimeError('下载地址返回网页，请重新解析或登录')
                total = int(response.headers.get('Content-Length') or 0)
                if status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if not match or int(match[1]) != size or int(match[2]) + 1 != int(match[3]):
                        raise RuntimeError('服务器返回的续传范围不匹配')
                    total = int(match[3])
                    remote_validator = response.headers.get('ETag') or response.headers.get('Last-Modified')
                    if info.get('validator') and remote_validator and remote_validator != info['validator']:
                        partial.unlink(missing_ok=True)
                        remove_record(path, 'part', self.local_metadata)
                        raise RuntimeError('资源版本已变化，重新下载')
                else:
                    size = 0
                validator = response.headers.get('ETag') or response.headers.get('Last-Modified')
                write_record(path, 'part', {'source': source, 'validator': validator, 'total': total}, self.local_metadata)
                written, started, last_report = size, time.monotonic(), 0
                with partial.open('ab' if size else 'wb') as handle:
                    for chunk in response.iter_content(128 * 1024):
                        self.check_stop()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= .2:
                            speed = (written-size) / max(now-started, .001)
                            self.stats(received=written, total=total, speed=speed,
                                       eta=(total-written)/speed if total and speed else None)
                            if progress_cb:
                                self._stop_pct = min(99, int(written*100/total)) if total else 0
                                progress_cb(self._stop_pct)
                            last_report = now
                    handle.flush()
                    os.fsync(handle.fileno())
                if not written or (total and written != total):
                    raise RuntimeError('文件长度不完整，保留进度以便继续')
                self.check_stop()
                self._finish(partial, path, source, written)
                remove_record(path, 'part', self.local_metadata)
                self.stats(received=written, total=written, speed=0, eta=0)
                if progress_cb:
                    progress_cb(100)
                return
            except InterruptedError_:
                raise
            except (requests.RequestException, OSError, RuntimeError, ValueError) as error:
                if attempt == RETRY_TIMES-1:
                    message = re.sub(r'https?://\S+', '[下载地址]', str(error))
                    raise RuntimeError('下载失败，已保留进度：' + message) from error
                self.log('连接中断，准备重试（%d/%d）' % (attempt+1, RETRY_TIMES))
            finally:
                if response is not None:
                    response.close()
                self._slots.release()
            if self._stop.wait(1+attempt):
                raise InterruptedError_()

    def _finish(self, partial, final, source, size):
        os.replace(partial, final)
        mark_complete(final, source, self.local_metadata)

    def _fetch(self, url, desc='内容', is_text=False, retries=RETRY_TIMES):
        for attempt in range(retries):
            self.check_stop()
            self._acquire(self._slots)
            try:
                with self._session().get(url, timeout=(10, 15)) as response:
                    response.raise_for_status()
                    return response.text if is_text else response.content
            except requests.RequestException:
                if attempt == retries-1:
                    raise RuntimeError('无法获取%s，请稍后重试' % desc)
            finally:
                self._slots.release()
            if self._stop.wait(attempt+1):
                raise InterruptedError_()

    def _resolve_uri(self, uri, base_url):
        absolute = urljoin(base_url, uri)
        base, target = urlsplit(base_url), urlsplit(absolute)
        if not target.query and target.netloc == base.netloc and base.query:
            absolute = urlunsplit(target._replace(query=base.query))
        return absolute

    def _parse_variants(self, text, base_url):
        playlist = m3u8.loads(text)
        return [(p.stream_info.resolution[1] if p.stream_info.resolution else 0,
                 self._resolve_uri(p.uri, base_url)) for p in playlist.playlists]

    def _media_playlist(self, text, url, quality, depth=0):
        if depth > 5:
            raise RuntimeError('播放列表嵌套过深')
        playlist = m3u8.loads(text)
        if playlist.is_variant:
            variants = sorted(playlist.playlists, key=lambda p: (p.stream_info.resolution[1] if p.stream_info.resolution else 0, p.stream_info.bandwidth or 0))
            cap = {1: 480, 2: 720, 3: 100000}.get(config.QUALITY_RANK.get(quality, 2), 720)
            eligible = [v for v in variants if v.stream_info.resolution and v.stream_info.resolution[1] <= cap]
            chosen = eligible[-1] if eligible else variants[0]
            if not any(v.stream_info.resolution for v in variants):
                rank = config.QUALITY_RANK.get(quality, 2)
                chosen = variants[-1] if rank == 3 else variants[0] if rank == 1 else variants[min(1, len(variants)-1)]
            if chosen.stream_info.audio:
                raise RuntimeError('此 HLS 使用独立音轨，暂不支持；请尝试其他画质')
            child = self._resolve_uri(chosen.uri, url)
            return self._media_playlist(self._fetch(child, is_text=True), child, quality, depth+1)
        return playlist, url

    def _resolve_segments(self, text, base_url):
        playlist, url = self._media_playlist(text, base_url, config.QUALITY_OPTIONS[1])
        return [self._resolve_uri(seg.uri, url) for seg in playlist.segments]

    def _download_segment(self, url, path):
        skipped = completed(path, url, self.local_metadata)
        self._download_direct(url, path)
        return True, skipped

    def _is_complete(self, url, size):
        with self._session().get(url, headers={'Range': 'bytes=%d-' % size}, stream=True, timeout=(10, 15)) as response:
            return response.status_code == 416 and response.headers.get('Content-Range') == 'bytes */%d' % size

    def _download_hls(self, url, out_path, ffmpeg_path, progress_cb=None, quality=None):
        if not ffmpeg_path or not Path(ffmpeg_path).is_file():
            raise RuntimeError('缺少视频合并组件，请重新安装完整版本')
        parts = Path(str(out_path)+'.parts')
        parts.mkdir(parents=True, exist_ok=True)
        playlist, media_url = self._media_playlist(self._fetch(url, is_text=True), url, quality or config.QUALITY_OPTIONS[1])
        if not playlist.segments or not playlist.is_endlist:
            raise RuntimeError('播放列表为空或属于直播，暂不支持')
        if any(k and k.method != 'NONE' for k in playlist.keys):
            raise PaidCourseError('此视频包含加密分片，暂不支持')
        if any(seg.byterange for seg in playlist.segments):
            raise RuntimeError('此视频使用字节范围分片，暂不支持')
        sources = [self._resolve_uri(seg.uri, media_url) for seg in playlist.segments]
        signature = hashlib.sha256(json.dumps([identity(u) for u in sources]).encode()).hexdigest()[:16]
        active = parts / signature
        active.mkdir(exist_ok=True)
        started, done, count, mappings, seen_maps = time.monotonic(), 0, len(sources), {}, {}
        for seg in playlist.segments:
            if seg.init_section:
                if id(seg.init_section) in seen_maps:
                    continue
                init_url = self._resolve_uri(seg.init_section.uri, media_url)
                if seg.init_section.byterange:
                    raise RuntimeError('此视频初始化片段使用字节范围，暂不支持')
                if init_url not in mappings:
                    mappings[init_url] = 'init_%03d.mp4' % len(mappings)
                    init_manager = DownloadManager(self.session, self.log, self.progress, stop_event=self._stop,
                                                   request_slots=self._slots, local_metadata=True)
                    try:
                        init_manager._download_direct(init_url, active / mappings[init_url])
                    finally:
                        init_manager.close()
                seg.init_section.uri = mappings[init_url]
                seen_maps[id(seg.init_section)] = mappings[init_url]
        segment_local, children = threading.local(), []
        def download_one(index):
            if not hasattr(segment_local, 'manager'):
                segment_local.manager = DownloadManager(self.session, self.log, self.progress, stop_event=self._stop,
                                                        request_slots=self._slots, local_metadata=True)
                children.append(segment_local.manager)
            segment_local.manager._download_segment(sources[index], str(active / ('seg_%05d.ts' % index)))
        pool = ThreadPoolExecutor(max_workers=HLS_WORKERS)
        futures = [pool.submit(download_one, i) for i in range(count)]
        try:
            for future in as_completed(futures):
                future.result()
                self.check_stop()
                done += 1
                if progress_cb:
                    progress_cb(min(99, int(done*100/count)))
                received = sum(p.stat().st_size for p in active.glob('seg_*.ts'))
                elapsed = max(time.monotonic()-started, .001)
                self.stats(received=received, total=0, speed=received/elapsed, eta=elapsed*(count-done)/done)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            for child in children:
                child.close()
        for index, seg in enumerate(playlist.segments):
            seg.uri = 'seg_%05d.ts' % index
        (active/'local.m3u8').write_text(playlist.dumps(), encoding='utf-8')
        self.check_stop()
        self.state('merging')
        self.stats(speed=0, eta=None)
        self._acquire(self._merge_lock)
        try:
            self._ffmpeg_concat(str(active), out_path, ffmpeg_path)
        finally:
            self._merge_lock.release()
        mark_complete(out_path, identity(url), self.local_metadata)
        if not parts.is_symlink() and parts.resolve().parent == Path(out_path).resolve().parent:
            shutil.rmtree(parts)
        if progress_cb:
            progress_cb(100)

    def _ffmpeg_concat(self, tmp_dir, out_path, ffmpeg_path):
        folder, cmd = Path(tmp_dir), [ffmpeg_path, '-hide_banner', '-nostdin', '-y']
        local = folder/'local.m3u8'
        if local.exists():
            cmd += ['-protocol_whitelist', 'file,crypto,data', '-allowed_extensions', 'ALL', '-i', str(local)]
        else:
            files = sorted(folder.glob('*.ts'))
            if not files:
                raise RuntimeError('没有可合并的分片')
            listing = folder/'list.txt'
            listing.write_text(''.join("file '%s'\n" % p.resolve().as_posix().replace("'", "'\\''") for p in files), encoding='utf-8')
            cmd += ['-f', 'concat', '-safe', '0', '-i', str(listing)]
        temp = str(out_path)+'.merging.mp4'
        cmd += ['-c', 'copy', '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', temp]
        with (folder/'ffmpeg.log').open('w', encoding='utf-8') as errors:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            lines = queue.Queue()
            def drain():
                for line in proc.stdout:
                    lines.put(line.strip())
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            start = time.monotonic()
            try:
                while proc.poll() is None:
                    self.check_stop()
                    if time.monotonic()-start > 3600:
                        raise RuntimeError('视频合并超时，已保留分片')
                    try:
                        line = lines.get(timeout=.1)
                        if line.startswith('out_time='):
                            self.stats(merge_time=line.split('=', 1)[1])
                    except queue.Empty:
                        pass
            except Exception:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise
            finally:
                reader.join(timeout=2)
                proc.stdout.close()
        if proc.returncode or not Path(temp).exists() or not Path(temp).stat().st_size:
            raise RuntimeError('视频合并失败，分片已保留；详情：'+str(folder/'ffmpeg.log'))
        self.check_stop()
        os.replace(temp, out_path)
