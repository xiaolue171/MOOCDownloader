# -*- coding: utf-8 -*-
"""数据模型。"""
from dataclasses import dataclass, field


@dataclass
class Unit:
    """一个学习单元：视频 或 课件。"""
    unit_id: int = 0            # bizId (DWR 里的 .id)
    name: str = ''
    content_type: int = 1       # 1=视频 3=课件
    content_id: int = 0         # videoId / 文档 contentId
    duration: float = 0.0       # 秒（视频；解析阶段尽量补齐）
    lesson_name: str = ''
    chapter_name: str = ''
    chapter_index: int = 0
    lesson_index: int = 0
    unit_index: int = 0
    # ---- 下载阶段解析出的信息 ----
    video_format: str = ''      # hls / mp4 / flv
    video_url: str = ''
    quality_label: str = ''     # 如 高清(720P)
    size_bytes: int = 0         # 课件大小

    @property
    def is_video(self):
        return self.content_type == 1

    @property
    def is_doc(self):
        return self.content_type == 3

    @property
    def duration_text(self):
        if self.duration <= 0:
            return '—'
        m, s = divmod(int(self.duration), 60)
        h, m = divmod(m, 60)
        if h:
            return '%d:%02d:%02d' % (h, m, s)
        return '%d:%02d' % (m, s)

    @property
    def size_text(self):
        if self.size_bytes <= 0:
            return self.duration_text
        b = float(self.size_bytes)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if b < 1024 or unit == 'GB':
                return '%.1f%s' % (b, unit)
            b /= 1024
        return '%.1fGB' % b


@dataclass
class Lesson:
    lesson_id: int = 0
    name: str = ''
    units: list = field(default_factory=list)


@dataclass
class Chapter:
    chapter_id: int = 0
    name: str = ''
    lessons: list = field(default_factory=list)


@dataclass
class Course:
    course_id: int = 0
    term_id: int = 0
    name: str = ''
    chapters: list = field(default_factory=list)

    def all_units(self):
        for ch in self.chapters:
            for les in ch.lessons:
                for u in les.units:
                    yield u

    @property
    def total_duration(self):
        return sum(u.duration for u in self.all_units() if u.is_video)

    @property
    def total_duration_text(self):
        d = self.total_duration
        if d <= 0:
            return '—'
        m, s = divmod(int(d), 60)
        h, m = divmod(m, 60)
        if h:
            return '%d小时%02d分' % (h, m)
        return '%d分%02d秒' % (m, s)
