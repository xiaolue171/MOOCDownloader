# -*- coding: utf-8 -*-
"""中国大学MOOC 免费公开课下载器 — 入口。"""
import sys


def _hide_console():
    """开发运行（python main.py）时隐藏附带的命令行黑窗；打包 exe 无控制台，此调用无副作用。"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception:
        pass


def main():
    if '--webview-self-test' in sys.argv:
        import json
        import webview
        from pathlib import Path
        result = {}
        window = webview.create_window('MOOC 登录组件自检', html='<html><body>WebView2 ready</body></html>', hidden=True)
        def check(win):
            try:
                win.events.loaded.wait(20)
                result['webview2'] = 'WebView2 ready' in (win.evaluate_js('document.body.innerText') or '')
            except Exception as error:
                result['error'] = str(error)
            finally:
                win.destroy()
        webview.start(check, (window,), gui='edgechromium', private_mode=True)
        Path(sys.argv[sys.argv.index('--webview-self-test')+1]).write_text(json.dumps(result), encoding='utf-8')
        return
    if '--login-worker' in sys.argv or '--login-ipc-self-test' in sys.argv:
        from app.login_ipc import run_worker
        flag = '--login-worker' if '--login-worker' in sys.argv else '--login-ipc-self-test'
        opener = None if flag == '--login-worker' else lambda: {
            'ok': True, 'cookies': {'test': 'dummy-ipc-secret'}, 'user': {'id': 'test', 'nickName': '通信自检'}}
        run_worker(sys.argv[sys.argv.index(flag)+1], opener)
        return
    import tkinter as tk
    from app.ui import MainWindow
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    if '--self-test' in sys.argv:
        import json
        from pathlib import Path
        from app import config
        root.withdraw()
        app = MainWindow(root)
        root.update_idletasks()
        result = {'version': config.APP_VERSION, 'ffmpeg': bool(config.find_ffmpeg()),
                  'pages': list(app.pages), 'tk': root.tk.call('info', 'patchlevel'),
                  'queue_actions': [app.remove_button.cget('text'), app.clear_button.cget('text')]}
        Path(sys.argv[sys.argv.index('--self-test')+1]).write_text(json.dumps(result), encoding='utf-8')
        root.destroy()
        return
    MainWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
