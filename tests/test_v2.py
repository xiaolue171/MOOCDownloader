"""Deterministic v2 regressions. No external network or user account access."""
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config
from app.downloader import DownloadManager, InterruptedError_, completed
from app.icourse_api import CourseClient, ApiError, ResourceUnavailable
from app.models import Course, Unit, Chapter, Lesson
from app.storage import QueueStore, write_json
from app.tasks import TaskEngine, make_task, output_path
from app.transfer_state import read_record, record_path, mark_complete, relocate_complete

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / '_test_tmp' / 'v2'
TEMP.mkdir(parents=True, exist_ok=True)


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.closed = data, status, False
        self.headers = {'Content-Length': str(len(data)), **(headers or {})}
        self.text = data.decode('utf-8', errors='replace')
        self.content = data
    def iter_content(self, size):
        for i in range(0, len(self.data), 4):
            yield self.data[i:i+4]
    def close(self):
        self.closed = True
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP failure')
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.path = Path(self.temp.name)
        assert self.path.resolve().is_relative_to(TEMP.resolve())
        self.environment = patch.dict(os.environ, {'MOOC_DATA_DIR': str(self.path/'appdata')})
        self.environment.start()
    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()
    def manager(self, response):
        dm = DownloadManager(None, lambda *a: None, lambda *a: None)
        dm._local.session = NS(get=lambda *a, **k: response)
        return dm


class Transfers(Base):
    def test_nonempty_is_not_complete(self):
        path = self.path/'video.mp4'
        path.write_bytes(b'bad')
        dm = self.manager(Response(b'complete file'))
        self.assertTrue(dm._download_video(Unit(), 'https://x/a', 'mp4', '', str(path), ''))
        self.assertEqual(path.read_bytes(), b'complete file')
        self.assertTrue(completed(path, 'https://x/a?ak=1'))
        self.assertFalse(list(path.parent.glob('*.json')))
        self.assertTrue(record_path(path, 'complete').is_file())

    def test_pause_then_resume(self):
        path = self.path/'video.mp4'
        dm = self.manager(Response(b'abcdefghijklmno'))
        dm.stats = lambda **kw: dm.stop()
        with self.assertRaises(InterruptedError_):
            dm._download_direct('https://x/a', path)
        self.assertFalse(path.exists())
        self.assertEqual(Path(str(path)+'.part').read_bytes(), b'abcd')
        dm = self.manager(Response(b'efghijklmno', 206, {'Content-Range': 'bytes 4-14/15'}))
        dm._download_direct('https://x/a?token=refreshed', path)
        self.assertEqual(path.read_bytes(), b'abcdefghijklmno')
        self.assertFalse(list(path.parent.glob('*.json')))

    def test_legacy_record_moves_out_of_course_folder(self):
        path = self.path/'video.mp4'
        path.write_bytes(b'finished')
        sidecar = Path(str(path)+'.complete.json')
        write_json(sidecar, {'source':'https://x/a', 'size':8})
        self.assertTrue(completed(path))
        self.assertFalse(sidecar.exists())
        self.assertEqual(read_record(path,'complete')['size'],8)

    def test_modified_same_size_file_is_not_complete(self):
        path = self.path/'video.mp4'
        self.manager(Response(b'finished'))._download_direct('https://x/a',path)
        before = path.stat().st_mtime_ns
        path.write_bytes(b'changed!')
        os.utime(path,ns=(before+2000000000,before+2000000000))
        self.assertFalse(completed(path))

    def test_unrelated_json_is_preserved(self):
        path=self.path/'video.mp4'
        path.write_bytes(b'personal file')
        sidecar=Path(str(path)+'.complete.json')
        write_json(sidecar,{'notes':'user metadata'})
        self.assertFalse(completed(path))
        self.assertTrue(sidecar.exists())

    def test_ignored_range_stays_bounded(self):
        path = self.path/'data'
        Path(str(path)+'.part').write_bytes(b'x'*8)
        write_json(str(path)+'.part.json', {'source': 'https://x/a'})
        values = []
        self.manager(Response(b'1234567890'))._download_direct('https://x/a', path, progress_cb=values.append)
        self.assertLessEqual(max(values), 100)
        self.assertEqual(path.read_bytes(), b'1234567890')

    def test_bad_416_restarts(self):
        path = self.path/'data'
        Path(str(path)+'.part').write_bytes(b'x'*20)
        write_json(str(path)+'.part.json', {'source': 'https://x/a'})
        first, second = Response(b'', 416, {'Content-Range': 'bytes */10'}), Response(b'0123456789')
        dm = self.manager(first)
        responses = iter([first, second])
        dm._local.session.get = lambda *a, **k: next(responses)
        with patch.object(dm._stop, 'wait', return_value=False):
            dm._download_direct('https://x/a', path)
        self.assertTrue(first.closed)
        self.assertEqual(path.read_bytes(), b'0123456789')

    def test_truncated_body_never_finishes(self):
        path = self.path/'data'
        dm = self.manager(Response(b'abc', headers={'Content-Length': '10'}))
        with patch.object(dm._stop, 'wait', return_value=False), self.assertRaises(RuntimeError):
            dm._download_direct('https://x/a', path)
        self.assertFalse(completed(path))

    def test_stop_before_generator(self):
        calls = []
        def tasks():
            calls.append(True)
            yield ()
        dm = self.manager(Response(b''))
        dm.stop()
        dm.run(tasks(), '')
        self.assertFalse(calls)

    def test_hls_failure_keeps_cache(self):
        target = self.path/'out.mp4'
        parts = Path(str(target)+'.parts')
        parts.mkdir()
        (parts/'saved.ts').write_bytes(b'existing segment')
        dm = self.manager(Response(b''))
        dm._fetch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))
        with self.assertRaises(RuntimeError):
            dm._download_hls('https://x/a', str(target), config.find_ffmpeg())
        self.assertEqual((parts/'saved.ts').read_bytes(), b'existing segment')


class API(Base):
    def doc_client(self, sources=None, token_code=0, detail_code=0):
        client = CourseClient()
        client.warmup = lambda: None
        doc = {'docId': 123, 'signature': 'test-signature', 'status': 'SUCCESS', 'fileName': '讲义.pdf'}
        client._post = lambda *a, **kw: NS(json=lambda: {'code': token_code, 'result': {'docSignDto': doc}})
        detail = {'fileName': '讲义.pdf', 'sources': sources if sources is not None else [
            {'type': 4, 'encryptPdfUrl': 'https://x/encrypted.pdf'},
            {'type': 3, 'url': 'http://nos.netease.com/pdf/lesson?download=lecture.pdf&Signature=test'}]}
        client._get = lambda *a, **kw: NS(json=lambda: {'code': detail_code, 'result': detail})
        client._dwr = lambda *a, **kw: self.fail('Handouts must not call the retired DWR endpoint')
        return client

    def test_doc_signed_pdf_source(self):
        client = self.doc_client()
        unit = Unit(unit_id=42, content_id=123, content_type=3)
        with patch.object(client, '_post', wraps=client._post) as post, patch.object(client, '_get', wraps=client._get) as get:
            url, name = client.resolve_doc(unit)
            self.assertEqual(post.call_args.kwargs['data']['contentType'], 3)
            self.assertEqual(post.call_args.kwargs['data']['bizId'], 42)
            self.assertEqual(get.call_args.kwargs['params'], {'signature':'test-signature', 'clientType':1})
        self.assertTrue(url.startswith('https://nos.netease.com/pdf/lesson'))
        self.assertEqual(name, 'lecture.pdf')

    def test_doc_denied_token_is_not_bypassed(self):
        client = self.doc_client(token_code=403)
        with patch.object(client, '_get') as get, self.assertRaises(ResourceUnavailable):
            client.resolve_doc(Unit(content_type=3))
        get.assert_not_called()

    def test_doc_preview_is_not_saved_as_pdf(self):
        client = self.doc_client(sources=[{'type':1, 'url':'https://x/preview.swf'},
                                          {'type':4, 'encryptPdfUrl':'https://x/encrypted.pdf'}])
        with self.assertRaises(ResourceUnavailable):
            client.resolve_doc(Unit(content_type=3))

    def test_doc_expired_signature_is_retryable(self):
        client = self.doc_client(detail_code=-2)
        with self.assertRaisesRegex(ApiError, '授权已过期') as raised:
            client.resolve_doc(Unit(content_type=3))
        self.assertNotIsInstance(raised.exception, ResourceUnavailable)

    def test_resource_permission_error_is_readable(self):
        client=CourseClient()
        client._post=lambda *a,**k: NS(text='dwr.engine._remoteHandleException(\'1\',\'0\',{message:"\\u6ca1\\u6709\\u6743\\u9650"});')
        with self.assertRaisesRegex(ResourceUnavailable,'没有权限'):
            client._dwr('getLessonUnitLearnVo',[])
    def test_quality_exact_tier(self):
        items = [{'videoUrl': 'https://x/a_shd.mp4', 'format':'mp4'}, {'videoUrl':'https://x/a_hd.mp4', 'format':'mp4'}]
        url, _, label = CourseClient._pick_quality(items, '高清优先')
        self.assertTrue(url.endswith('_hd.mp4'))
        self.assertNotIn('720', label)

    def test_quality_actual_height(self):
        item = {'videoUrl':'https://x/a_hd.mp4', 'format':'mp4', 'height':540}
        self.assertEqual(CourseClient._pick_quality([item], '高清优先')[2], '540P')

    def test_term_url_wins(self):
        client = CourseClient()
        client.warmup = lambda: None
        client._get_page = lambda *a: 'termId:222'
        client._dwr = lambda *a: {'name':'test'}
        self.assertEqual(client.load_course('https://www.icourse163.org/learn/T-1?tid=999').term_id, 999)

    def test_transient_error_keeps_auth(self):
        client = CourseClient()
        client.session.cookies.set('NTES_SESS', 'dummy')
        client._resolve_video = lambda *a: (_ for _ in ()).throw(ApiError('network failure'))
        with self.assertRaises(ApiError):
            client.get_video_urls(Unit(), '高清优先')
        self.assertTrue(client._has_auth_cookies())

    def test_master_resolution(self):
        dm = self.manager(Response(b''))
        self.assertEqual(dm._parse_variants('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=1920x1080\na.m3u8\n', 'https://x/master')[0][0],1080)

    def test_reject_unrelated_site(self):
        with self.assertRaises(ApiError):
            CourseClient.parse_course_url('https://example.org/course/X-123')


class Tasks(Base):
    def test_names_and_formats(self):
        c = Course(course_id=1, term_id=2, name='course')
        a = make_task(c, Unit(unit_id=1, unit_index=1, name='same', chapter_name='chapter', lesson_name='lesson'), str(self.path), '高清优先')
        b = make_task(c, Unit(unit_id=2, unit_index=2, name='same', chapter_name='chapter', lesson_name='lesson'), str(self.path), '高清优先')
        self.assertNotEqual(output_path(a,'.mp4'), output_path(b,'.mp4'))
        self.assertEqual(Path(output_path(a,'.mp4')).name, '01 same.mp4')
        self.assertTrue(output_path(a,'.flv').endswith('.flv'))
        self.assertEqual(config.sanitize_filename('CON'), '_CON')

    def legacy_task(self):
        course = Course(course_id=1,term_id=2,name='course')
        unit = Unit(unit_id=1308050832,unit_index=1,name='恒星为什么发光？',chapter_name='chapter',lesson_name='lesson')
        task = make_task(course,unit,str(self.path/'课程'),'最高画质')
        task.update(path=output_path(task,'.mp4',legacy=True),status='done')
        path=Path(task['path'])
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'video contents')
        write_json(str(path)+'.complete.json',{'source':'https://x/a','size':path.stat().st_size})
        store=QueueStore(self.path/'queue.json')
        store.tasks[task['id']]=task
        store.save()
        return course,unit,task,store

    def test_existing_downloads_renamed_without_redownload(self):
        course,unit,task,store=self.legacy_task()
        old=Path(task['path'])
        engine=TaskEngine(lambda:self.fail('No network client expected'),lambda e:None,store)
        new=Path(store.tasks[task['id']]['path'])
        self.assertEqual(new.name,'01 恒星为什么发光？.mp4')
        self.assertEqual(new.read_bytes(),b'video contents')
        self.assertFalse(old.exists())
        self.assertFalse(list(new.parent.glob('*.json')))
        self.assertTrue(completed(new))
        self.assertEqual(engine.add(course,[unit],task['directory'],task['quality']),[])
        self.assertTrue(list((self.path/'appdata'/'backups').glob('queue-before-clean-names-*.json')))
        self.assertEqual(engine.organize_downloads()['renamed_or_recovered'],0)

    def test_existing_filename_is_not_overwritten_during_cleanup(self):
        _,_,task,store=self.legacy_task()
        existing=Path(output_path(task,'.mp4'))
        existing.write_bytes(b'keep my file')
        TaskEngine(lambda:None,lambda e:None,store)
        self.assertEqual(existing.read_bytes(),b'keep my file')
        new=Path(store.tasks[task['id']]['path'])
        self.assertEqual(new.name,'01 恒星为什么发光？ (2).mp4')
        self.assertEqual(new.read_bytes(),b'video contents')

    def test_cleanup_recovers_after_rename_before_queue_save(self):
        _,_,task,store=self.legacy_task()
        old=Path(task['path'])
        new=Path(output_path(task,'.mp4'))
        relocate_complete(old,new,task['id'])
        engine=TaskEngine(lambda:None,lambda e:None,store)
        self.assertEqual(store.tasks[task['id']]['path'],str(new))
        self.assertTrue(completed(new))
        self.assertEqual(engine.organization_result['renamed_or_recovered'],1)

    def test_quality_variants_and_queued_names_are_reserved(self):
        store=QueueStore(self.path/'queue.json')
        engine=TaskEngine(lambda:None,lambda e:None,store)
        c=Course(course_id=1,term_id=2,name='course')
        u=Unit(unit_id=1,unit_index=1,name='same',chapter_name='chapter',lesson_name='lesson')
        first=engine.add(c,[u],str(self.path/'课程'),'高清优先')[0]
        second=engine.add(c,[u],str(self.path/'课程'),'最高画质')[0]
        a=engine._reserve_path(first,'.mp4')
        b=engine._reserve_path(second,'.mp4')
        self.assertEqual(Path(a).name,'01 same.mp4')
        self.assertEqual(Path(b).name,'01 same (2).mp4')
        self.assertEqual(engine._reserve_path(second,'.mp4'),b)

    def test_legacy_partial_resumes_then_receives_clean_name(self):
        _,_,task,store=self.legacy_task()
        old=Path(task['path'])
        old.unlink()
        Path(str(old)+'.complete.json').unlink()
        Path(str(old)+'.part').write_bytes(b'abcd')
        write_json(str(old)+'.part.json',{'source':'https://x/a','total':8})
        task['status']='paused'
        store.save()
        client=CourseClient()
        client.get_video_urls=lambda *a:('https://x/a','mp4','',0)
        engine=TaskEngine(lambda:client,lambda e:None,store)
        calls=[]
        def get(url,**kwargs):
            calls.append(kwargs['headers'])
            return Response(b'efgh',206,{'Content-Range':'bytes 4-7/8'})
        with patch.object(DownloadManager,'_session',return_value=NS(get=get)), patch.object(TaskEngine,'_probe_quality',return_value=''):
            engine.start([task['id']])
            engine.thread.join(5)
        result=store.tasks[task['id']]
        self.assertEqual(result['status'],'done',result['error'])
        self.assertEqual(calls[0]['Range'],'bytes=4-')
        path=Path(result['path'])
        self.assertEqual(path.name,'01 恒星为什么发光？.mp4')
        self.assertEqual(path.read_bytes(),b'abcdefgh')
        self.assertFalse(list(path.parent.glob('*.json')))

    def test_failed_resolution_terminal(self):
        client = CourseClient()
        client.get_video_urls = lambda *a: (_ for _ in ()).throw(ApiError('failed'))
        store = QueueStore(self.path/'queue.json')
        engine = TaskEngine(lambda: client, lambda event: None, store)
        ids = engine.add(Course(course_id=1,term_id=2,name='course'), [Unit(unit_id=1)], str(self.path), '高清优先')
        engine.start(ids)
        engine.thread.join(6)
        self.assertFalse(engine.running)
        self.assertEqual(store.tasks[ids[0]]['status'], 'failed')

    def test_restore_inflight_as_paused(self):
        path = self.path/'queue.json'
        write_json(path, [{'id':'one','status':'merging'}])
        self.assertEqual(QueueStore(path).tasks['one']['status'], 'paused')

    def test_real_ffmpeg_merge_quoted_unicode_path(self):
        folder = self.path/"课程's segments"
        folder.mkdir()
        segment = folder/'a.ts'
        result = subprocess.run([config.find_ffmpeg(), '-y', '-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10',
                                 '-t','1','-c:v','libx264','-f','mpegts',str(segment)], capture_output=True,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        out = self.path/'out.mp4'
        dm = self.manager(Response(b''))
        dm._ffmpeg_concat(str(folder), str(out), config.find_ffmpeg())
        self.assertTrue(out.stat().st_size > 0)

    def test_hls_local_playlist_pipeline(self):
        folder = self.path/'hls source'
        folder.mkdir()
        generated = subprocess.run([config.find_ffmpeg(), '-y','-v','error','-f','lavfi','-i','color=c=red:s=160x90:r=10',
                                    '-t','3','-c:v','libx264','-g','10','-hls_time','1','-hls_list_size','0',
                                    '-hls_segment_filename',str(folder/'s%02d.ts'),str(folder/'index.m3u8')],
                                   capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(generated.returncode,0)
        states=[]
        import requests
        def session_factory():
            def get(url, **kwargs):
                return Response((folder/url.rsplit('/',1)[1]).read_bytes())
            return NS(headers={},cookies=requests.cookies.RequestsCookieJar(),proxies={},trust_env=False,get=get,close=lambda:None)
        with patch('app.downloader.requests.Session', side_effect=session_factory):
            dm=DownloadManager(None,lambda *a:None,lambda *a:None,state_cb=states.append)
            out=self.path/'视频.mp4'
            dm._download_hls('https://x/index.m3u8',str(out),config.find_ffmpeg())
            dm.close()
        self.assertTrue(completed(out,'https://x/index.m3u8'))
        self.assertIn('merging',states)
        self.assertFalse(Path(str(out)+'.parts').exists())
        self.assertFalse(list(out.parent.glob('*.json')))

    def test_encrypted_cookie_storage(self):
        from app.cookies import CookieManager
        path=self.path/'account.json'
        cm=CookieManager(path)
        cm.save({'NTES_SESS':'dummy-secret'}, {'id':'42','nickName':'测试'})
        self.assertNotIn('dummy-secret',path.read_text())
        restored=CookieManager(path)
        self.assertEqual(restored.cookies['NTES_SESS'],'dummy-secret')
        self.assertFalse(restored.logged_in(),'Restored identities must be verified again')
        cm.clear()
        self.assertFalse(path.exists())

    def test_login_worker_without_console(self):
        from app.login_ipc import run_worker, read_result
        path = self.path/'login-result.json'
        expected = {'ok': True, 'cookies': {'NTES_SESS': 'dummy-ipc-secret'}, 'user': {'id': '42', 'nickName': '测试'}}
        with patch('sys.stdout', None), patch('sys.stderr', None):
            run_worker(path, lambda: expected)
        self.assertNotIn('dummy-ipc-secret', path.read_text())
        self.assertEqual(read_result(path), expected)
        def fail():
            raise RuntimeError('模拟窗口错误')
        run_worker(path, fail)
        self.assertFalse(read_result(path)['ok'])
        self.assertEqual(read_result(path)['message'], '模拟窗口错误')


if __name__ == '__main__':
    unittest.main(verbosity=2)
