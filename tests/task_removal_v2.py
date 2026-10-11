"""Removal regressions with real transfer code and deterministic fake responses."""
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_v2 import Base, Response
from app.downloader import DownloadManager, completed
from app.icourse_api import CourseClient
from app.models import Course, Unit
from app.storage import QueueStore, read_json, write_json
from app.tasks import TaskEngine
from app.transfer_state import mark_complete


class Removal(Base):
    def prepare(self):
        course = Course(course_id=1, term_id=2, name='test')
        units = [Unit(unit_id=i, unit_index=i, name='课件%d' % i, content_type=3,
                      chapter_name='章', lesson_name='节') for i in range(1, 4)]
        events = []
        def client():
            value = CourseClient()
            value.resolve_doc = lambda unit: ('https://x/%d' % unit.unit_id, 'handout.pdf')
            return value
        engine = TaskEngine(client, events.append, QueueStore(self.path/'queue.json'))
        ids = engine.add(course, units, str(self.path/'courses'), '高清优先')
        return course, units, engine, ids, events

    @contextmanager
    def blocked_queue(self):
        course, units, engine, ids, events = self.prepare()
        ready, release = threading.Event(), threading.Event()
        class Blocked(Response):
            def iter_content(inner, size):
                yield b'abcd'
                ready.set()
                if not release.wait(8):
                    raise RuntimeError('Test timed out waiting for removal')
                yield b'efgh'
        def get(url, **kwargs):
            return Blocked(b'abcdefgh') if url.endswith('/1') else Response(b'abcdefgh')
        with patch.object(DownloadManager, '_session', return_value=NS(get=get)):
            engine.start(ids, workers=1)
            try:
                self.assertTrue(ready.wait(5))
                yield course, units, engine, ids, events, release
            finally:
                release.set()
                engine.thread.join(6)
                self.assertFalse(engine.running)

    def test_remove_finished_keep_file_then_readd_skips_without_network(self):
        course, units, engine, ids, _ = self.prepare()
        path = Path(engine._reserve_path(ids[0], '.pdf'))
        path.parent.mkdir(parents=True)
        path.write_bytes(b'finished pdf')
        mark_complete(path, 'https://x/1')
        engine.update(ids[0], status='done')
        self.assertEqual(engine.remove([ids[0], ids[0], 'missing']), 1)
        store = QueueStore(engine.store.path)
        self.assertNotIn(ids[0], store.tasks)
        self.assertEqual(path.read_bytes(), b'finished pdf')
        restored = TaskEngine(lambda: self.fail('Completed file must not download again'), lambda e: None, store)
        restored.add(course, [units[0]], str(self.path/'courses'), '高清优先')
        restored.start([ids[0]])
        restored.thread.join(5)
        self.assertFalse(restored.running)
        self.assertEqual(store.tasks[ids[0]]['status'], 'skipped')
        self.assertEqual(store.tasks[ids[0]]['path'], str(path))

    def test_remove_selected_active_and_queued_keeps_others_and_resumes(self):
        with self.blocked_queue() as (course, units, engine, ids, events, release):
            path = Path(engine.store.tasks[ids[0]]['path'])
            self.assertEqual(engine.remove(ids[:2]), 2)
            self.assertEqual(engine.store.tasks[ids[0]]['status'], 'removing')
            self.assertNotIn(ids[1], engine.store.tasks)
            self.assertEqual(engine.add(course, [units[0]], str(self.path/'courses'), '高清优先'), [])
            # A new record with the same ID must not run in the old worker slot.
            engine.add(course, [units[1]], str(self.path/'courses'), '高清优先')
            release.set()
            engine.thread.join(5)
        self.assertNotIn(ids[0], engine.store.tasks)
        self.assertEqual(engine.store.tasks[ids[1]]['status'], 'waiting')
        self.assertEqual(engine.store.tasks[ids[2]]['status'], 'done')
        self.assertFalse(any(e[0] == 'error' for e in events), events)
        self.assertEqual(Path(str(path)+'.part').read_bytes(), b'abcd')
        restored = TaskEngine(engine.client_factory, events.append, QueueStore(engine.store.path))
        restored.add(course, [units[0]], str(self.path/'courses'), '高清优先')
        headers = []
        def get(url, **kwargs):
            headers.append(kwargs['headers'])
            return Response(b'efgh', 206, {'Content-Range':'bytes 4-7/8'})
        with patch.object(DownloadManager, '_session', return_value=NS(get=get)):
            restored.start([ids[0]])
            restored.thread.join(5)
        self.assertEqual(headers[0]['Range'], 'bytes=4-')
        self.assertEqual(restored.store.tasks[ids[0]]['status'], 'done')
        self.assertEqual(path.read_bytes(), b'abcdefgh')
        self.assertFalse(list(path.parent.glob('*.json')))

    def test_clear_running_queue_never_resurrects_and_preserves_completed_files(self):
        with self.blocked_queue() as (_, _, engine, ids, events, release):
            path = Path(engine._reserve_path(ids[2], '.pdf'))
            path.write_bytes(b'finished pdf')
            mark_complete(path, 'https://x/3')
            engine.update(ids[2], status='done')
            self.assertEqual(engine.remove(), 3)
            self.assertEqual(engine.remove(), 0)
            release.set()
            engine.thread.join(5)
        self.assertEqual(engine.store.snapshot(), [])
        self.assertEqual(QueueStore(engine.store.path).snapshot(), [])
        self.assertTrue(completed(path))
        engine.update(ids[0], status='downloading', percent=66)
        engine.pause(ids)
        self.assertFalse(engine.start(ids))
        self.assertEqual(engine.store.snapshot(), [])
        self.assertFalse(any(e[0] == 'error' for e in events), events)

    def test_removing_one_of_two_active_downloads_keeps_other_running(self):
        _, _, engine, ids, events = self.prepare()
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        class Blocked(Response):
            def __init__(inner, index):
                super().__init__(b'abcdefgh')
                inner.index = index
            def iter_content(inner, size):
                yield b'abcd'
                entered[inner.index].set()
                if not release.wait(8):
                    raise RuntimeError('Test timed out waiting for removal')
                yield b'efgh'
        def get(url, **kwargs):
            index = int(url.rsplit('/', 1)[1])-1
            return Blocked(index) if index < 2 else Response(b'abcdefgh')
        with patch.object(DownloadManager, '_session', return_value=NS(get=get)):
            engine.start(ids, workers=2)
            try:
                self.assertTrue(all(event.wait(5) for event in entered))
                engine.remove([ids[0]])
                self.assertFalse(engine.controls[ids[1]].is_set())
                self.assertEqual(engine.store.tasks[ids[1]]['status'], 'downloading')
            finally:
                release.set()
                engine.thread.join(6)
        self.assertFalse(engine.running)
        self.assertNotIn(ids[0], engine.store.tasks)
        self.assertEqual(engine.store.tasks[ids[1]]['status'], 'done')
        self.assertEqual(engine.store.tasks[ids[2]]['status'], 'done')
        self.assertEqual(Path(engine.store.tasks[ids[1]]['path']).read_bytes(), b'abcdefgh')
        self.assertFalse(any(e[0] == 'error' for e in events), events)

    def test_removal_interrupted_by_exit_is_finished_on_restart(self):
        _, _, engine, ids, _ = self.prepare()
        path = engine._reserve_path(ids[0], '.pdf')
        engine.update(ids[0], status='removing')
        restored = QueueStore(engine.store.path)
        self.assertNotIn(ids[0], restored.tasks)
        self.assertEqual(restored.files[ids[0]], path)
        self.assertEqual(len(restored.snapshot()), 2)
        self.assertEqual(len(QueueStore(engine.store.path).snapshot()), 2)

    def test_save_failure_keeps_queue_records(self):
        _, _, engine, ids, _ = self.prepare()
        engine._reserve_path(ids[0], '.pdf')
        def write(path, value):
            if Path(path) == engine.store.path:
                raise OSError('Simulated disk error')
            write_json(path, value)
        with patch('app.storage.write_json', side_effect=write), self.assertRaises(OSError):
            engine.remove([ids[0]])
        self.assertIn(ids[0], engine.store.tasks)
        self.assertEqual(len(read_json(engine.store.path)), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
