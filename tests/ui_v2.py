"""UI regression and an optional isolated preview with clearly marked sample data."""
import os
import sys
import tkinter as tk
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['MOOC_DATA_DIR'] = str(ROOT/'_test_tmp'/'ui_v2_data')
from app.ui import MainWindow
from app.models import Course, Chapter, Lesson, Unit
from app.tasks import make_task

root = tk.Tk()
root.withdraw()
if '--large-fonts' in sys.argv:
    root.tk.call('tk', 'scaling', 2.0)
app = MainWindow(root)
app.course = Course(course_id=1, term_id=2, name='普通天文学 · 界面预览示例')
for index, name in enumerate(['认识天体物理学', '观测恒星与宇宙', '探索宇宙的起源'], 1):
    units = [Unit(unit_id=index*10, name=['什么是天体物理学？','如何测量恒星的距离？','什么是暴胀宇宙？'][index-1],
                  chapter_name=name, lesson_name='视频与配套课件', duration=500, chapter_index=index, lesson_index=1, unit_index=1),
             Unit(unit_id=index*10+1, name='本节课程讲义', content_type=3, chapter_name=name,
                  lesson_name='视频与配套课件', size_bytes=2600000, chapter_index=index, lesson_index=1, unit_index=2)]
    app.course.chapters.append(Chapter(chapter_id=index, name=name, lessons=[Lesson(lesson_id=index, name='视频与配套课件', units=units)]))
app.units = {u.unit_id:u for u in app.course.all_units()}
app.course_title.configure(text=app.course.name)
app.course_info.configure(text='3 章 · 3 个视频 · 3 份课件（仅用于界面测试）')
app.url.set('https://www.icourse163.org/course/NJU-1001752349')
app._render_tree()
app._select('all')
app.view_mode.set('仅课件')
app._render_tree()
assert len(app.selected) == 6
app._select('none')
app.view_mode.set('全部')
app._render_tree()
assert not app.selected
app._select('video')
assert len(app.selected) == 3
for iid in list(app.node_members):
    if iid.startswith('l:'):
        app.tree.item(iid, open=True)
for index,u in enumerate(app.course.all_units()):
    task=make_task(app.course,u,str(ROOT/'_test_tmp'/'ui_downloads'),'高清优先')
    task.update(status=['downloading','done','paused','failed','waiting','done'][index],
                percent=[62,100,35,0,0,100][index], received=40000000 if u.is_video else 2600000,
                total=64571931 if u.is_video else 2600000,
                speed=1048576 if index==0 else 0, eta=24 if index==0 else None,
                actual_quality='540P · 960×540' if u.is_video else '',
                error='示例：网络暂时不可用，可重试' if index==3 else '')
    app.engine.store.tasks[task['id']]=task
app._render_tasks()
app.task_filter.set('已完成')
app._render_tasks()
assert len(app.record_tree.get_children())==2
app.task_filter.set('全部')
app._render_tasks()
assert len(app.record_tree.get_children())==6
from unittest.mock import patch
with patch.object(app.engine, 'start') as start:
    app._start_waiting()
    started_ids = start.call_args.args[0]
    assert len(started_ids) == 1
    assert all(app.engine.store.tasks[key]['status'] == 'waiting' for key in started_ids)
root.update_idletasks()
assert len(app.pages)==3
if '--preview' in sys.argv:
    if '--small' in sys.argv:
        root.geometry('900x620')
    root.deiconify()
    root.title('MOOC 下载器 · 新版界面测试')
    root.mainloop()
else:
    keys = list(app.record_tree.get_children())
    app.record_tree.selection_set(keys[:2])
    app._task_detail()
    assert not app.remove_button.instate(['disabled'])
    stale = dict(app.engine.store.tasks[keys[0]])
    app._remove_selected()
    assert len(app.engine.store.snapshot()) == 4
    assert not app.record_tree.exists(keys[0])
    app.events.put(('task', stale))
    app._poll()
    assert not app.record_tree.exists(keys[0]), 'Delayed worker event resurrected a removed row'
    app.task_filter.set('已完成')
    app._render_tasks()
    with patch('app.ui.messagebox.askyesno', return_value=False):
        app._clear_tasks()
    assert len(app.engine.store.snapshot()) == 4, 'Cancelling clear must preserve all tasks'
    with patch('app.ui.messagebox.askyesno', return_value=True):
        app._clear_tasks()
    assert not app.engine.store.snapshot(), 'Clear must include filtered-out tasks'
    assert not app.record_tree.get_children()
    app._poll()
    assert not app.record_tree.get_children()
    assert app.clear_button.instate(['disabled']) and app.remove_button.instate(['disabled'])
    for page in app.pages:
        app.show_page(page)
        root.update_idletasks()
    print('UI checks OK: selection, filters, continuation, multi-remove, clear/cancel, stale events, 3 pages')
    for timer in root.tk.call('after', 'info'):
        root.after_cancel(timer)
    root.destroy()
