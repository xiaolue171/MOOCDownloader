# -*- coding: utf-8 -*-
"""
Win32 原生"选择文件夹"对话框
============================
tkinter.filedialog.askdirectory 在打包后的 windowed 应用 / 与 WebView2
共存等场景下偶发卡死（模态对话框与 Tk 主循环冲突），这里改用系统原生
SHBrowseForFolderW，独立消息循环，绝不阻塞 Tk；失败时调用方回退
tkinter.filedialog。
"""
import ctypes
from ctypes import wintypes

BIF_RETURNONLYFSDIRS = 0x00000001
BIF_NEWDIALOGSTYLE = 0x00000040

BFFM_INITIALIZED = 1
BFFM_SETSELECTION = 0x0402    # WM_USER + 2

# Win32 回调必须是 stdcall（WINFUNCTYPE），不能用 cdecl 的 CFUNCTYPE
_BFFCALLBACK = ctypes.WINFUNCTYPE(
    ctypes.c_int, wintypes.HWND, wintypes.UINT, wintypes.LPARAM, wintypes.LPARAM)


class _BROWSEINFO(ctypes.Structure):
    _fields_ = [
        ('hwndOwner', wintypes.HWND),
        ('pidlRoot', ctypes.c_void_p),
        ('pszDisplayName', wintypes.LPWSTR),
        ('lpszTitle', wintypes.LPCWSTR),
        ('ulFlags', wintypes.UINT),
        ('lpfn', _BFFCALLBACK),
        ('lParam', wintypes.LPARAM),
        ('iImage', ctypes.c_int),
    ]


def ask_directory(title='选择保存文件夹', initial_dir=''):
    """弹出原生文件夹选择框，返回所选路径字符串；取消/失败返回 None。"""
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        user32 = ctypes.windll.user32
    except AttributeError:
        return None

    # ctypes 默认 restype=int 会截断 64 位 PIDL 指针，必须显式声明
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BROWSEINFO)]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    user32.SendMessageW.restype = ctypes.c_ssize_t   # LRESULT
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]

    ole32.CoInitialize(None)
    try:
        initial = ctypes.c_wchar_p(initial_dir)

        @_BFFCALLBACK
        def _callback(hwnd, msg, lparam, data):
            if msg == BFFM_INITIALIZED and initial_dir:
                # BFFM_SETSELECTION: wParam=1 表示 lParam 为字符串指针
                user32.SendMessageW(hwnd, BFFM_SETSELECTION, 1,
                                    ctypes.cast(initial, ctypes.c_void_p).value)
            return 0

        display = ctypes.create_unicode_buffer(512)
        bi = _BROWSEINFO()
        bi.hwndOwner = 0
        bi.pszDisplayName = ctypes.cast(display, ctypes.c_wchar_p)  # 不能直接赋数组
        bi.lpszTitle = title
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
        bi.lpfn = _callback

        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if pidl:
            buf = ctypes.create_unicode_buffer(1024)
            if shell32.SHGetPathFromIDListW(pidl, buf):
                return buf.value
    except Exception:
        return None
    finally:
        try:
            ole32.CoUninitialize()
        except Exception:
            pass
    return None
