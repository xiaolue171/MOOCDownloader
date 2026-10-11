"""Explicit live regression: download first/middle/last handouts as a guest."""
import json
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.icourse_api import CourseClient
from app.tasks import TaskEngine
from app.storage import QueueStore
from app.downloader import completed

work=ROOT/'_test_tmp'/'handouts-v201'
work.mkdir(parents=True,exist_ok=True)
client=CourseClient()
course=client.load_course('https://www.icourse163.org/course/NJU-1001752349')
client.session.close()
docs=[u for u in course.all_units() if u.is_doc]
selected=[docs[0],docs[len(docs)//2],docs[-1]]
store=QueueStore(work/'queue.json')
engine=TaskEngine(CourseClient,lambda event: None,store)
ids=engine.add(course,selected,str(work/'课程'),'高清优先')
started=time.monotonic()
if ids:
    engine.start(ids,workers=2)
    engine.thread.join(120)
    if engine.running:
        engine.pause()
        engine.thread.join(35)
        raise AssertionError('Timed out downloading handouts')
results=[]
for task in store.snapshot():
    assert task['status'] in ('done','skipped'),task.get('error')
    path=Path(task['path'])
    assert completed(path)
    content=path.read_bytes()
    assert content.startswith(b'%PDF-'), 'Not a PDF: '+path.name
    assert b'%%EOF' in content[-1024:], 'Missing PDF trailer: '+path.name
    results.append({'unit_id':task['unit']['unit_id'],'name':task['name'],'status':task['status'],
                    'bytes':len(content),'pdf_signature':True,'pdf_trailer':True,'path':str(path)})
evidence={'course':course.name,'total_handouts':len(docs),'guest':True,
          'elapsed_seconds':round(time.monotonic()-started,2),'samples':results}
(work/'results.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(evidence,ensure_ascii=False,indent=2))
