"""Check real Tk geometry at small window sizes and common Windows font scales."""
import json
import os
import sys
from pathlib import Path
import tkinter as tk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['MOOC_DATA_DIR'] = str(ROOT/'_test_tmp'/'layout-v2-data')
from app.ui import MainWindow

def fully_visible(widget):
    assert widget.winfo_ismapped(), 'Control is not mapped: '+str(widget)
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    right, bottom = x+widget.winfo_width(), y+widget.winfo_height()
    parent = widget.master
    while parent is not None:
        assert x >= parent.winfo_rootx() and y >= parent.winfo_rooty(), 'Control clipped at top/left'
        assert right <= parent.winfo_rootx()+parent.winfo_width(), 'Control clipped at right: '+str(widget)
        assert bottom <= parent.winfo_rooty()+parent.winfo_height(), 'Control clipped at bottom: '+str(widget)
        parent = parent.master

results = []
for percent in (100, 125, 150):
    root = tk.Tk()
    root.attributes('-alpha', 0)
    root.tk.call('tk', 'scaling', (96/72)*(percent/100))
    app = MainWindow(root)
    app.selection_label.configure(text='已选 43 个视频 · 43 份课件')
    for width, height in ((900,620), (980,680), (1160,780)):
        root.geometry('%dx%d' % (width,height))
        app.show_page('course')
        root.update()
        fully_visible(app.enqueue_button)
        fully_visible(app.quality_combo)
        fully_visible(app.parse_button)
        assert app.enqueue_button.winfo_height() >= app.enqueue_button.winfo_reqheight()
        assert app.tree.winfo_height() >= 68, ('Resource list has no visible row',percent,width,height,app.tree.winfo_height())
        app.show_page('downloads')
        root.update()
        fully_visible(app.record_tree)
        fully_visible(app.remove_button)
        fully_visible(app.clear_button)
        for button in (app.remove_button, app.clear_button):
            assert button.winfo_width() >= button.winfo_reqwidth(), 'Task management button text clipped'
        results.append({'scale_percent':percent, 'window':[width,height], 'download_button_visible':True})
    for timer in root.tk.call('after', 'info'):
        root.after_cancel(timer)
    root.destroy()
(ROOT/'_test_tmp'/'layout-v2-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print('LAYOUT PASS', len(results), 'window/scale combinations')
