# -*- coding: utf-8 -*-
"""全局配置：路径、常量、UA。"""
import os
import sys
import shutil

APP_NAME = 'icourse163-downloader'
APP_TITLE = '中国大学MOOC 免费公开课下载器'
APP_VERSION = '2.0.2'

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)
HOME_URL = 'https://www.icourse163.org/'
REFERER = 'https://www.icourse163.org/'

# DWR / RPC 接口
DWR_ENDPOINT = 'https://www.icourse163.org/dwr/call/plaincall/CourseBean.getLastLearnedMocTermDto.dwr'
TOKEN_RPC = 'https://www.icourse163.org/web/j/resourceRpcBean.getResourceTokenV2.rpc'
VOD_API = 'https://vod.study.163.com/eds/api/v1/vod/video'
DOC_API = 'https://vod.study.163.com/api/v1/doc/{doc_id}/get'

# 内容类型
CT_VIDEO = 1   # 视频
CT_TEST = 2    # 单元测试等（忽略）
CT_DOC = 3     # 课件/文档

# 清晰度: 界面选项 -> (排序值, 关键词)
QUALITY_OPTIONS = ['标清优先', '高清优先', '最高画质']
QUALITY_RANK = dict(zip(QUALITY_OPTIONS, (1, 2, 3)))
QUALITY_RANK.update({'标清(360P)': 1, '高清(720P)': 2, '超清(1080P)': 3})
QUALITY_KEYWORDS = {
    1: ['sd', '360', '流畅', '标清'],
    2: ['hd', '720', '高清'],
    3: ['shd', '1080', '超清'],
}

DEFAULT_TASK_DIR_NAME = 'downloads'


def app_dir():
    """exe 所在目录（打包后）或项目根目录（开发时）。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bin_dir():
    return os.path.join(app_dir(), 'bin')


def find_ffmpeg():
    """优先使用 exe 同目录 bin\\ffmpeg.exe，其次系统 PATH。"""
    candidates = [os.path.join(bin_dir(), 'ffmpeg.exe'),
                  os.path.join(getattr(sys, '_MEIPASS', app_dir()), 'bin', 'ffmpeg.exe'),
                  os.path.join(bin_dir(), 'ffmpeg')]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which('ffmpeg')


def sanitize_filename(name):
    """去除 Windows 非法文件名字符。"""
    for ch in '\\/:*?"<>|\r\n\t':
        name = name.replace(ch, '_')
    name = name.strip(' .')[:48].rstrip(' .')
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM%d' % i for i in range(1, 10)} | {'LPT%d' % i for i in range(1, 10)}
    if name.split('.')[0].upper() in reserved:
        name = '_' + name
    return name or 'unnamed'


def data_dir():
    """Writable per-user data, independent of the installation directory."""
    return os.environ.get('MOOC_DATA_DIR') or os.path.join(
        os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'MOOCDownloader')


def default_download_dir():
    return os.path.join(os.path.expanduser('~'), 'Downloads', 'MOOC课程')
