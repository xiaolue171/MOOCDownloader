"""Compact Windows-style Tk interface, backed by a persistent task engine."""
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from . import config
from .cookies import CookieManager
from .login_ipc import read_result
from .icourse_api import CourseClient, LoginExpired
from .storage import Settings
from .tasks import TaskEngine, ACTIVE, FINISHED, STATUS_TEXT

BG, WHITE, TEXT, MUTED, BLUE = '#f3f4f6', '#ffffff', '#202020', '#646b74', '#0078d4'


def size_text(value):
    if not value:
        return '—'
    value = float(value)
    for suffix in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or suffix == 'GB':
            return '%.1f %s' % (value, suffix)
        value /= 1024


class MainWindow:
    def __init__(self, root):
        self.root = root
        root.title('MOOC 下载器')
        root.geometry('1160x780')
        root.minsize(900, 620)
        try:
            root.iconbitmap(str(Path(getattr(sys, '_MEIPASS', config.app_dir()))/'assets'/'app.ico'))
        except tk.TclError:
            pass
        root.configure(bg=BG)
        root.option_add('*Font', ('Microsoft YaHei UI', 10))
        self.events, self.settings, self.cookies = queue.Queue(), Settings(), CookieManager()
        self.course, self.selected, self.units, self.node_members = None, set(), {}, {}
        self.generation, self.parsing, self.login_process, self._closing = 0, False, None, False
        self.engine = TaskEngine(self._new_client, self.events.put)
        self.url = tk.StringVar(value=self.settings.values['last_url'])
        self.directory = tk.StringVar(value=self.settings.values['directory'])
        self.quality = tk.StringVar(value=self.settings.values['quality'])
        self.workers = tk.IntVar(value=self.settings.values['workers'])
        self.search, self.view_mode, self.task_filter = tk.StringVar(), tk.StringVar(value='全部'), tk.StringVar(value='全部')
        self.status = tk.StringVar(value='就绪 · 免费公开课可直接使用游客模式')
        self._styles()
        self._build()
        self._render_tasks()
        self._account_label()
        self.search.trace_add('write', lambda *args: self._render_tree())
        root.protocol('WM_DELETE_WINDOW', self._close)
        root.after(100, self._poll)
        if self.cookies.records:
            threading.Thread(target=self._verify_saved, daemon=True).start()

    def _styles(self):
        s = ttk.Style(self.root)
        s.theme_use('clam')
        s.configure('.', font=('Microsoft YaHei UI', 10), background=BG, foreground=TEXT)
        s.configure('TFrame', background=BG)
        s.configure('Card.TFrame', background=WHITE)
        s.configure('TLabel', background=BG)
        s.configure('Card.TLabel', background=WHITE)
        s.configure('Muted.TLabel', foreground=MUTED)
        s.configure('CardMuted.TLabel', background=WHITE, foreground=MUTED)
        s.configure('Title.TLabel', font=('Microsoft YaHei UI', 22, 'bold'))
        s.configure('CardTitle.TLabel', background=WHITE, font=('Microsoft YaHei UI', 12, 'bold'))
        s.configure('TButton', background=WHITE, borderwidth=1, bordercolor='#d3d7dc', padding=(12, 7), relief='flat')
        s.map('TButton', background=[('active', '#e5e9ee'), ('disabled', '#f1f2f4')], foreground=[('disabled', '#a0a5ad')])
        s.configure('Primary.TButton', background=BLUE, foreground=WHITE, bordercolor=BLUE)
        s.map('Primary.TButton', background=[('active', '#106ebe'), ('disabled', '#c8d8e8')], foreground=[('disabled', WHITE)])
        s.configure('Nav.TButton', background='#e9ebee', borderwidth=0, anchor='w', padding=(18, 13))
        s.map('Nav.TButton', background=[('active', '#dde3ea')])
        s.configure('Selected.Nav.TButton', background='#dcebf8', foreground='#0067b8')
        s.configure('TEntry', fieldbackground=WHITE, bordercolor='#c8cdd4', padding=7)
        s.configure('TCombobox', fieldbackground=WHITE, background=WHITE, padding=6)
        s.map('TCombobox', fieldbackground=[('readonly', WHITE)])
        s.configure('Treeview', background=WHITE, fieldbackground=WHITE, rowheight=34, borderwidth=0)
        s.configure('Treeview.Heading', background='#f6f7f9', foreground=MUTED, relief='flat', padding=(8, 8))
        s.map('Treeview', background=[('selected', '#e3f0fa')], foreground=[('selected', TEXT)])
        s.configure('Horizontal.TProgressbar', background=BLUE, troughcolor='#e3e7ed', borderwidth=0)

    def _button(self, frame, title, command, primary=False, side='left'):
        b = ttk.Button(frame, text=title, command=command, style='Primary.TButton' if primary else 'TButton')
        b.pack(side=side, padx=(0, 8))
        return b

    def _build(self):
        sidebar = tk.Frame(self.root, bg='#e9ebee', width=176)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text='M', bg=BLUE, fg=WHITE, font=('Segoe UI', 25, 'bold'), width=2).pack(anchor='w', padx=22, pady=(28, 8))
        tk.Label(sidebar, text='MOOC 下载器', bg='#e9ebee', fg=TEXT, font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', padx=20)
        tk.Label(sidebar, text='把课堂带在身边', bg='#e9ebee', fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=20, pady=(4, 28))
        self.nav, self.pages = {}, {}
        self.body = ttk.Frame(self.root, padding=(26, 22, 26, 12))
        self.body.pack(fill='both', expand=True)
        for key, text in [('course', '课程资源'), ('downloads', '下载任务'), ('settings', '设置')]:
            b = ttk.Button(sidebar, text=text, style='Nav.TButton', command=lambda k=key: self.show_page(k))
            b.pack(fill='x', pady=2, padx=8)
            self.nav[key] = b
            self.pages[key] = ttk.Frame(self.body)
        tk.Label(sidebar, text='版本 '+config.APP_VERSION, bg='#e9ebee', fg=MUTED, font=('Segoe UI', 9)).pack(side='bottom', anchor='w', padx=20, pady=20)
        status_label = ttk.Label(self.body, textvariable=self.status, style='Muted.TLabel', wraplength=870)
        status_label.pack(side='bottom', fill='x', pady=(10, 0))
        status_label.bind('<Configure>', lambda e: status_label.configure(wraplength=max(100, e.width)))
        self._course_page()
        self._download_page()
        self._settings_page()
        self.show_page('course')

    def show_page(self, key):
        for name, frame in self.pages.items():
            frame.pack_forget()
            self.nav[name].configure(style='Selected.Nav.TButton' if name == key else 'Nav.TButton')
        self.pages[key].pack(fill='both', expand=True)

    def _heading(self, page, title, subtitle):
        ttk.Label(page, text=title, style='Title.TLabel').pack(anchor='w')
        description = ttk.Label(page, text=subtitle, style='Muted.TLabel')
        description.pack(anchor='w', pady=(4, 20))
        return description

    def _course_page(self):
        page = self.pages['course']
        page.columnconfigure(0, weight=1)
        page.rowconfigure(5, weight=1)
        heading = ttk.Frame(page)
        heading.grid(row=0, column=0, sticky='ew')
        self.course_subtitle = self._heading(heading, '课程资源', '粘贴课程链接，选择需要的视频与课件。')
        card = ttk.Frame(page, style='Card.TFrame', padding=16)
        card.grid(row=1, column=0, sticky='ew', pady=(0, 14))
        ttk.Label(card, text='中国大学 MOOC 课程链接', style='CardMuted.TLabel').pack(anchor='w', pady=(0, 8))
        row = ttk.Frame(card, style='Card.TFrame')
        row.pack(fill='x')
        entry = ttk.Entry(row, textvariable=self.url)
        entry.pack(side='left', fill='x', expand=True, padx=(0, 12))
        entry.bind('<Return>', lambda event: self._parse())
        self.parse_button = self._button(row, '解析课程', self._parse, True)
        self.course_title = ttk.Label(card, text='尚未载入课程', style='CardTitle.TLabel')
        self.course_title.pack(anchor='w', pady=(16, 3))
        self.course_info = ttk.Label(card, text='支持课程页面与带学期参数的学习页面链接', style='CardMuted.TLabel')
        self.course_info.pack(anchor='w')
        bar = ttk.Frame(page)
        bar.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        for title, mode in [('全选课程', 'all'), ('清空选择', 'none'), ('仅选视频', 'video'), ('仅选课件', 'doc')]:
            self._button(bar, title, lambda m=mode: self._select(m))
        search_row = ttk.Frame(page)
        search_row.grid(row=3, column=0, sticky='ew', pady=(0, 8))
        combo = ttk.Combobox(search_row, textvariable=self.view_mode, values=['全部', '仅视频', '仅课件'], state='readonly', width=8)
        combo.pack(side='right', padx=(10, 0))
        combo.bind('<<ComboboxSelected>>', lambda e: self._render_tree())
        search = ttk.Entry(search_row, textvariable=self.search)
        search.pack(side='left', fill='x', expand=True)
        self.search_hint = ttk.Label(page, text='上方可搜索资源名称 · 筛选不会改变已选资源', style='Muted.TLabel')
        self.search_hint.grid(row=4, column=0, sticky='w', pady=(0, 6))
        frame = ttk.Frame(page, style='Card.TFrame')
        frame.grid(row=5, column=0, sticky='nsew')
        self.tree = ttk.Treeview(frame, columns=('check', 'name', 'type', 'duration'), show='tree headings')
        self.tree.column('#0', width=24, stretch=False)
        for col, title, width in [('check', '', 38), ('name', '资源名称', 420), ('type', '类型', 70), ('duration', '时长 / 大小', 110)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, stretch=col == 'name', anchor='w' if col == 'name' else 'center')
        scroll = ttk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.tag_configure('chapter', foreground='#1e5f91', font=('Microsoft YaHei UI', 10, 'bold'))
        self.tree.bind('<Button-1>', self._tree_click)
        self.tree.bind('<space>', self._tree_space)
        bottom = ttk.Frame(page, padding=(0, 12, 0, 0))
        bottom.grid(row=6, column=0, sticky='ew')
        bottom.columnconfigure(0, weight=1)
        self.selection_label = ttk.Label(bottom, text='未选择资源', wraplength=180)
        self.selection_label.grid(row=0, column=0, sticky='w', padx=(0, 8))
        self.quality_combo = ttk.Combobox(bottom, textvariable=self.quality, values=config.QUALITY_OPTIONS, state='readonly', width=10)
        self.quality_combo.grid(row=0, column=1, padx=(0, 12))
        self.enqueue_button = ttk.Button(bottom, text='加入下载队列', command=self._enqueue, style='Primary.TButton')
        self.enqueue_button.grid(row=0, column=2, sticky='e')
        self._course_compact = None
        page.bind('<Configure>', self._adapt_course_layout)

    def _adapt_course_layout(self, event):
        compact = event.height < 650
        if compact == self._course_compact:
            return
        self._course_compact = compact
        if compact:
            self.course_subtitle.pack_forget()
            self.search_hint.grid_remove()
        else:
            self.course_subtitle.pack(anchor='w', pady=(4, 20))
            self.search_hint.grid()

    def _download_page(self):
        page = self.pages['downloads']
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)
        heading = ttk.Frame(page)
        heading.grid(row=0, column=0, sticky='ew')
        self._heading(heading, '下载任务', '进度自动保存，暂停或重启后可以继续。')
        bar = ttk.Frame(page)
        bar.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        self._button(bar, '继续队列', self._resume, True)
        self._button(bar, '暂停所选', lambda: self.engine.pause(self.record_tree.selection()))
        self._button(bar, '全部暂停', self._pause_all)
        self._button(bar, '重试失败', self._retry)
        self._button(bar, '打开文件夹', self._open_task_folder)
        manage = ttk.Frame(page)
        manage.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        self.remove_button = self._button(manage, '移除所选任务', self._remove_selected)
        self.clear_button = self._button(manage, '清空下载记录', self._clear_tasks)
        combo = ttk.Combobox(manage, textvariable=self.task_filter, values=['全部', '进行中', '已完成', '已暂停', '失败 / 不可下载'], state='readonly', width=14)
        combo.pack(side='right')
        combo.bind('<<ComboboxSelected>>', lambda e: self._render_tasks())
        frame = ttk.Frame(page, style='Card.TFrame')
        frame.grid(row=3, column=0, sticky='nsew')
        columns = [('name', '资源', 235), ('quality', '画质 / 类型', 150), ('status', '状态', 82),
                   ('progress', '进度', 65), ('size', '已下载 / 总大小', 145), ('speed', '速度', 100), ('eta', '剩余时间', 90)]
        self.record_tree = ttk.Treeview(frame, columns=[x[0] for x in columns], show='headings', selectmode='extended')
        for col, title, width in columns:
            self.record_tree.heading(col, text=title)
            self.record_tree.column(col, width=width, minwidth=60, stretch=col=='name', anchor='w')
        vs = ttk.Scrollbar(frame, orient='vertical', command=self.record_tree.yview)
        hs = ttk.Scrollbar(frame, orient='horizontal', command=self.record_tree.xview)
        self.record_tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side='right', fill='y')
        hs.pack(side='bottom', fill='x')
        self.record_tree.pack(fill='both', expand=True)
        self.record_tree.tag_configure('failed', foreground='#b42318')
        self.record_tree.tag_configure('done', foreground='#107c41')
        self.record_tree.bind('<<TreeviewSelect>>', self._task_detail)
        self.task_summary = ttk.Label(page, text='还没有下载任务', style='Muted.TLabel')
        self.task_summary.grid(row=4, column=0, sticky='w', pady=(12, 6))
        self.detail = ttk.Label(page, text='Ctrl / Shift 可多选；移除记录会保留文件和下载进度', wraplength=850)
        self.detail.grid(row=5, column=0, sticky='ew', pady=(0, 8))
        self.detail.bind('<Configure>', lambda e: self.detail.configure(wraplength=max(100, e.width)))
        self.logs = tk.Text(page, height=3, bg=WHITE, fg=MUTED, relief='flat', padx=12, pady=8, state='disabled', font=('Microsoft YaHei UI', 9))
        self.logs.grid(row=6, column=0, sticky='ew')

    def _settings_page(self):
        page = self.pages['settings']
        self._heading(page, '设置', '简洁的默认设置，也保留需要时的选择。')
        card = ttk.Frame(page, style='Card.TFrame', padding=22)
        card.pack(fill='x', pady=(0, 16))
        ttk.Label(card, text='下载位置', style='CardTitle.TLabel').pack(anchor='w', pady=(0, 10))
        row = ttk.Frame(card, style='Card.TFrame')
        row.pack(fill='x')
        ttk.Entry(row, textvariable=self.directory).pack(side='left', fill='x', expand=True, padx=(0, 12))
        self._button(row, '选择文件夹', self._browse)
        row = ttk.Frame(card, style='Card.TFrame')
        row.pack(fill='x', pady=(20, 8))
        ttk.Label(row, text='同时下载的资源数', style='Card.TLabel').pack(side='left', padx=(0, 20))
        ttk.Combobox(row, textvariable=self.workers, values=[1, 2, 3, 4], width=5, state='readonly').pack(side='left')
        ttk.Label(card, text='建议 2 个。更高并发不一定更快；画质按源站可用档位选择，下载后显示实际分辨率。', style='CardMuted.TLabel', wraplength=790).pack(anchor='w', pady=(8, 14))
        self._button(card, '保存设置', self._save_settings, True)
        account = ttk.Frame(page, style='Card.TFrame', padding=22)
        account.pack(fill='x', pady=(0, 16))
        ttk.Label(account, text='账户（可选）', style='CardTitle.TLabel').pack(anchor='w')
        self.account_text = ttk.Label(account, style='CardMuted.TLabel', wraplength=790)
        self.account_text.pack(anchor='w', pady=(8, 16))
        row = ttk.Frame(account, style='Card.TFrame')
        row.pack(fill='x')
        self.login_button = self._button(row, '登录中国大学 MOOC', self._login)
        self._button(row, '退出账户', self._logout)
        ttk.Label(page, text='视频合并组件：'+('已就绪' if config.find_ffmpeg() else '未找到，请安装完整版本'), style='Muted.TLabel').pack(anchor='w', pady=8)
        ttk.Label(page, text='设置与任务保存在当前 Windows 用户目录；卸载软件不会删除已下载的课程。', style='Muted.TLabel', wraplength=830).pack(anchor='w')

    def _new_client(self):
        client = CourseClient(self.cookies.build_session() if self.cookies.logged_in() else None)
        client.web_user_id = str(self.cookies.user.get('id', '')) if self.cookies.logged_in() else ''
        client.auth_changed = lambda: self.events.put(('auth_expired',))
        return client

    def _parse(self):
        if self.parsing:
            return
        url = self.url.get().strip()
        if not url:
            self.status.set('请先粘贴课程链接')
            return
        self.generation += 1
        generation = self.generation
        self.parsing = True
        self.parse_button.configure(text='解析中…', state='disabled')
        self.status.set('正在读取课程目录…')
        def work():
            client = self._new_client()
            try:
                self.events.put(('course', generation, client.load_course(url)))
            except Exception as error:
                self.events.put(('parse_error', generation, str(error)))
            finally:
                client.session.close()
        threading.Thread(target=work, daemon=True).start()

    def _render_tree(self):
        opened = {iid for iid in self.node_members if self.tree.exists(iid) and self.tree.item(iid, 'open')}
        self.tree.delete(*self.tree.get_children())
        self.node_members = {}
        if not self.course:
            return
        search, mode = self.search.get().strip().casefold(), self.view_mode.get()
        for ch in self.course.chapters:
            cid, inserted = 'c:%s' % ch.chapter_id, False
            for les in ch.lessons:
                visible = [u for u in les.units if (not search or search in u.name.casefold() or search in les.name.casefold())
                           and not (mode == '仅视频' and u.is_doc) and not (mode == '仅课件' and u.is_video)]
                if not visible:
                    continue
                if not inserted:
                    self.tree.insert('', 'end', iid=cid, values=('', ch.name, '', ''), open=True, tags=('chapter',))
                    self.node_members[cid] = {u.unit_id for lesson in ch.lessons for u in lesson.units}
                    inserted = True
                lid = 'l:%s' % les.lesson_id
                self.tree.insert(cid, 'end', iid=lid, values=('', les.name, '', ''), open=(lid in opened or bool(search)))
                self.node_members[lid] = {u.unit_id for u in les.units}
                for u in visible:
                    iid = 'u:%s' % u.unit_id
                    self.tree.insert(lid, 'end', iid=iid, values=('', u.name, '视频' if u.is_video else '课件', u.size_text))
                    self.node_members[iid] = {u.unit_id}
        self._selection_changed()

    def _select(self, mode):
        self.selected = {uid for uid, u in self.units.items() if mode == 'all' or (mode=='video' and u.is_video) or (mode=='doc' and u.is_doc)}
        self._selection_changed()

    def _selection_changed(self):
        for iid, members in self.node_members.items():
            count = len(members & self.selected)
            self.tree.set(iid, 'check', '■' if count == len(members) else '◩' if count else '□')
        videos = sum(self.units[uid].is_video for uid in self.selected)
        self.selection_label.configure(text='已选 %d 个视频 · %d 份课件' % (videos, len(self.selected)-videos))

    def _toggle(self, iid):
        members = self.node_members.get(iid, set())
        if members and members <= self.selected:
            self.selected -= members
        else:
            self.selected |= members
        self._selection_changed()

    def _tree_click(self, event):
        if self.tree.identify_column(event.x) == '#1':
            self._toggle(self.tree.identify_row(event.y))
            return 'break'

    def _tree_space(self, event):
        for iid in self.tree.selection():
            self._toggle(iid)
        return 'break'

    def _enqueue(self):
        if not self.course or not self.selected:
            self.status.set('请先解析课程并选择要下载的资源')
            return
        if not self._save_settings():
            return
        units = [u for u in self.course.all_units() if u.unit_id in self.selected]
        try:
            ids = self.engine.add(self.course, units, self.directory.get(), self.quality.get())
            self._render_tasks()
            self.show_page('downloads')
            self.status.set('已加入 %d 项任务；已完整下载的资源会自动跳过' % len(ids))
            if not self.engine.running:
                self.engine.start(workers=self.workers.get())
        except Exception as error:
            messagebox.showerror('无法创建任务', str(error))

    def _accept_task(self, task):
        mode, status = self.task_filter.get(), task['status']
        return (mode=='全部' or (mode=='进行中' and status in ACTIVE) or (mode=='已完成' and status in FINISHED)
                or (mode=='已暂停' and status=='paused') or (mode=='失败 / 不可下载' and status in ('failed', 'unavailable')))

    def _task_row(self, task):
        iid = task['id']
        # Worker events can arrive after a removal or a re-add. Always display
        # current state rather than putting a stale event back into the list.
        with self.engine.store.lock:
            task = dict(self.engine.store.tasks.get(iid, {}))
        if not task:
            if self.record_tree.exists(iid):
                self.record_tree.delete(iid)
            return
        eta = task.get('eta')
        remaining = ('%d 分 %02d 秒' % divmod(int(eta), 60)) if eta is not None and eta > 0 else '—'
        values = (task['name'], task.get('actual_quality') or ('课件' if task['kind']=='doc' else task['quality']),
                  STATUS_TEXT.get(task['status'], task['status']), '%d%%' % task.get('percent', 0),
                  size_text(task.get('received'))+' / '+size_text(task.get('total')),
                  size_text(task.get('speed'))+'/s' if task.get('speed') else '—', remaining)
        if not self._accept_task(task):
            if self.record_tree.exists(iid):
                self.record_tree.delete(iid)
        elif self.record_tree.exists(iid):
            self.record_tree.item(iid, values=values, tags=(task['status'],))
        else:
            self.record_tree.insert('', 'end', iid=iid, values=values, tags=(task['status'],))

    def _render_tasks(self):
        tasks = self.engine.store.snapshot()
        keys = {task['id'] for task in tasks}
        for iid in self.record_tree.get_children():
            if iid not in keys:
                self.record_tree.delete(iid)
        for task in tasks:
            self._task_row(task)
        self._summary()
        self._task_detail()

    def _summary(self):
        tasks = self.engine.store.snapshot()
        count = lambda statuses: sum(t['status'] in statuses for t in tasks)
        self.task_summary.configure(text='%d 项任务   ·   完成 %d   ·   进行中 %d   ·   暂停 %d   ·   失败 / 不可下载 %d' %
                                    (len(tasks), count(FINISHED), count(ACTIVE), count({'paused'}), count({'failed', 'unavailable'})))
        self.clear_button.configure(state='normal' if any(t['status'] != 'removing' for t in tasks) else 'disabled')

    def _task_detail(self, event=None):
        ids = self.record_tree.selection()
        with self.engine.store.lock:
            tasks = [dict(self.engine.store.tasks[key]) for key in ids if key in self.engine.store.tasks]
        self.remove_button.configure(state='normal' if any(t['status'] != 'removing' for t in tasks) else 'disabled')
        if len(tasks) > 1:
            self.detail.configure(text='已选择 %d 项任务；移除记录会保留文件和下载进度' % len(tasks))
        elif tasks:
            self.detail.configure(text=(tasks[0].get('error') or tasks[0].get('path') or '等待获取资源地址'))
        else:
            self.detail.configure(text='Ctrl / Shift 可多选；移除记录会保留文件和下载进度')

    def _remove_selected(self):
        ids = self.record_tree.selection()
        if ids:
            self._remove_tasks(ids)

    def _clear_tasks(self):
        ids = [t['id'] for t in self.engine.store.snapshot() if t['status'] != 'removing']
        if ids and messagebox.askyesno('清空下载记录',
                '清空全部 %d 项任务记录？（包含当前筛选未显示的任务）\n\n'
                '正在运行的任务会先停止。已下载文件和未完成的下载进度会保留，重新添加相同任务可以继续。' % len(ids),
                parent=self.root):
            self._remove_tasks(ids)

    def _remove_tasks(self, ids):
        try:
            count = self.engine.remove(ids)
            self._render_tasks()
            self.status.set('已请求移除 %d 项记录；运行中的任务将在停止后移除，文件和进度已保留' % count)
        except OSError as error:
            self._render_tasks()
            messagebox.showerror('无法移除记录', str(error), parent=self.root)

    def _pause_all(self):
        self.engine.pause()
        self.status.set('正在暂停任务并保留进度；正在等待网络响应的任务可能需要数秒')

    def _resume(self):
        if self.engine.running:
            self.status.set('请等待当前任务暂停完成，再继续队列')
            return
        self.engine.start(workers=self.workers.get())

    def _start_waiting(self):
        if self._closing or self.engine.running:
            return
        ids = [t['id'] for t in self.engine.store.snapshot() if t['status'] == 'waiting']
        self.engine.start(ids, workers=self.workers.get())

    def _retry(self):
        if self.engine.running:
            self.status.set('请先暂停当前队列，再重试失败项')
            return
        ids = [t['id'] for t in self.engine.store.snapshot() if t['status'] in ('failed', 'unavailable')]
        self.engine.start(ids, self.workers.get())

    def _open_task_folder(self):
        ids = self.record_tree.selection()
        path = self.directory.get()
        if ids:
            task = self.engine.store.tasks.get(ids[0], {})
            path = str(Path(task['path']).parent) if task.get('path') else task.get('directory', path)
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            os.startfile(path)
        except OSError as error:
            messagebox.showerror('打开文件夹', str(error))

    def _browse(self):
        directory = filedialog.askdirectory(parent=self.root, initialdir=self.directory.get(), title='选择课程保存位置')
        if directory:
            self.directory.set(directory)
            self._save_settings()

    def _save_settings(self):
        try:
            directory = os.path.abspath(os.path.expanduser(self.directory.get().strip()))
            if not self.directory.get().strip():
                raise ValueError('请选择保存文件夹')
            self.settings.save(directory=directory, quality=self.quality.get(), workers=self.workers.get(), last_url=self.url.get().strip())
            self.directory.set(directory)
            self.status.set('设置已保存')
            return True
        except (OSError, ValueError, tk.TclError) as error:
            messagebox.showerror('保存设置失败', str(error))
            return False

    def _account_label(self):
        if self.cookies.logged_in():
            text = '已验证登录：'+str(self.cookies.user.get('nickName') or 'MOOC 用户')
        elif self.cookies.records:
            text = '正在验证保存的账户信息；网络异常时可退出账户，使用游客模式。'
        else:
            text = '游客模式 · 免费公开课通常无需登录。遇到明确的登录要求时再使用账户。'
        self.account_text.configure(text=text)

    def _verify_saved(self):
        client = CourseClient(self.cookies.build_session())
        try:
            self.events.put(('verified', client.verify_identity()))
        except LoginExpired:
            self.events.put(('auth_expired',))
        except Exception:
            self.events.put(('log', '暂时无法验证账户，请检查网络或使用游客模式。'))
        finally:
            client.session.close()

    def _login(self):
        if self.engine.running or self.parsing:
            self.status.set('请等待课程解析或下载暂停完成后再切换账户')
            return
        self.login_button.configure(state='disabled', text='登录窗口已打开…')
        result_path = Path(config.data_dir()) / ('login-result-' + uuid.uuid4().hex + '.json')
        command = [sys.executable] + ([] if getattr(sys, 'frozen', False) else [str(Path(config.app_dir())/'main.py')]) + ['--login-worker', str(result_path)]
        def work():
            try:
                self.login_process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.login_process.wait(timeout=600)
                result = read_result(result_path)
                if result.get('ok'):
                    candidate = CookieManager(str(Path(config.data_dir())/'unused-candidate.json'))
                    candidate.records = result.get('records') or [{'name': k, 'value': v, 'domain': '.icourse163.org', 'path': '/'} for k, v in result.get('cookies', {}).items()]
                    client = CourseClient(candidate.build_session())
                    try:
                        user = client.verify_identity()
                    finally:
                        client.session.close()
                    self.events.put(('login_ok', candidate.records, user))
                else:
                    self.events.put(('login_error', result.get('message') or '登录窗口已关闭'))
            except Exception as error:
                if self.login_process and self.login_process.poll() is None:
                    self.login_process.terminate()
                    self.login_process.wait(timeout=5)
                self.events.put(('login_error', str(error)))
            finally:
                self.login_process = None
                result_path.unlink(missing_ok=True)
        threading.Thread(target=work, daemon=True).start()

    def _logout(self):
        if self.engine.running or self.parsing:
            self.status.set('请先暂停下载并等待解析结束，再退出账户')
            return
        try:
            self.cookies.clear()
            self._account_label()
            self.status.set('已退出账户，后续请求使用游客模式')
        except OSError as error:
            messagebox.showerror('退出账户', str(error))

    def _poll(self):
        updates = {}
        for _ in range(500):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == 'task':
                updates[event[1]['id']] = event[1]
            elif kind == 'removed':
                for key in event[1]:
                    updates[key] = {'id': key}
            elif kind in ('course', 'parse_error') and event[1] == self.generation:
                self.parsing = False
                self.parse_button.configure(text='解析课程', state='normal')
                if kind == 'course':
                    self.course, self.selected = event[2], set()
                    self.units = {u.unit_id: u for u in self.course.all_units()}
                    self.course_title.configure(text=self.course.name)
                    videos = sum(u.is_video for u in self.units.values())
                    self.course_info.configure(text='%d 章  ·  %d 个视频  ·  %d 份课件' % (len(self.course.chapters), videos, len(self.units)-videos))
                    self._render_tree()
                    self.status.set('解析完成，请选择需要的资源')
                else:
                    self.status.set('解析失败：'+event[2])
            elif kind == 'queue_done':
                self.status.set('本轮任务结束；失败或暂停的项目可继续处理')
                if not self._closing and any(t['status']=='waiting' for t in self.engine.store.snapshot()):
                    self.root.after(200, self._start_waiting)
            elif kind == 'verified':
                self.cookies.user, self.cookies.verified = event[1], True
                self._account_label()
            elif kind == 'auth_expired':
                self.cookies.verified = False
                self.engine.pause()
                self.account_text.configure(text='登录已失效。请退出账户使用游客模式，或重新登录。')
                self.status.set('登录状态已变化，队列已暂停以避免混用身份')
            elif kind == 'login_ok':
                try:
                    self.cookies.save(event[1], event[2])
                    self._account_label()
                    self.status.set('登录已验证，信息已由 Windows 加密保存')
                except Exception as error:
                    self.status.set('保存登录信息失败：'+str(error))
                self.login_button.configure(state='normal', text='登录中国大学 MOOC')
            elif kind == 'login_error':
                self.login_button.configure(state='normal', text='登录中国大学 MOOC')
                self.status.set(event[1])
            elif kind in ('log', 'error'):
                self.logs.configure(state='normal')
                self.logs.insert('end', time.strftime('%H:%M:%S')+'  '+event[1]+'\n')
                if int(self.logs.index('end-1c').split('.')[0]) > 150:
                    self.logs.delete('1.0', '30.0')
                self.logs.see('end')
                self.logs.configure(state='disabled')
        for task in updates.values():
            self._task_row(task)
        if updates:
            self._summary()
            self._task_detail()
        if not self._closing:
            self.root.after(200, self._poll)

    def _close(self):
        self._closing = True
        self.engine.pause()
        if self.login_process and self.login_process.poll() is None:
            self.login_process.terminate()
        self.status.set('正在保存任务进度…')
        def finish():
            if self.engine.running:
                self.root.after(150, finish)
                return
            self.engine.store.save()
            self._save_settings()
            self.root.destroy()
        finish()
