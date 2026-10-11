"""Opt-in public course integration: pause/resume one video plus one handout."""
import json
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.icourse_api import CourseClient
from app.storage import QueueStore
from app.tasks import TaskEngine
from app.downloader import completed

work=ROOT/'_test_tmp'/'v2_live'
work.mkdir(parents=True,exist_ok=True)
client=CourseClient()
course=client.load_course('https://www.icourse163.org/course/NJU-1001752349')
client.session.close()
video=next(u for u in course.all_units() if u.is_video)
doc=next(u for u in course.all_units() if u.is_doc)
store=QueueStore(work/'queue.json')
seen=set()
pause=[True]
def events(event):
    if event[0]=='task':
        task=event[1]
        key=(task['id'],task['status'])
        if key not in seen:
            seen.add(key)
            print(task['kind'],task['status'],task.get('percent'),flush=True)
        if pause[0] and task.get('received',0)>=1024*1024:
            pause[0]=False
            engine.pause()
engine=TaskEngine(CourseClient,events,store)
ids=engine.add(course,[video],str(work/'课程'),'高清优先')
assert ids,'Use a fresh test directory for this integration'
engine.start(ids,1)
engine.thread.join(150)
assert not engine.running,'Pause did not finish within the test budget'
first=store.tasks[ids[0]]
assert first['status']=='paused',first['status']
partial=Path(first['path']+'.part')
assert partial.exists() and partial.stat().st_size>0
resumed_bytes=partial.stat().st_size
engine.add(course,[doc],str(work/'课程'),'高清优先')
start=time.monotonic()
engine.start(workers=2)
engine.thread.join(200)
if engine.running:
    engine.pause()
    engine.thread.join(35)
results={'resume_from_bytes':resumed_bytes,'elapsed_after_resume':time.monotonic()-start,'tasks':store.snapshot()}
(work/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
for task in store.snapshot():
    assert task['status'] in ('done','skipped'),(task['status'],task.get('error'))
    assert completed(task['path'])
print('LIVE PASS',json.dumps({'resume_from_bytes':resumed_bytes,'tasks':[(t['kind'],t['status'],t.get('actual_quality'),t['total']) for t in store.snapshot()]},ensure_ascii=False),flush=True)
