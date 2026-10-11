# -*- coding: utf-8 -*-
"""
登录模块
========
使用 pywebview 打开 icourse163 登录窗口，使用系统安装的 WebView2 运行时，
无需打包浏览器内核。

pywebview 6 要求 webview.start() 在主线程调用（窗口消息循环），而传入的
func 会在独立线程执行。因此：主线程切给 webview 跑消息循环，后台线程轮询
登录状态，成功后调 win.destroy() 关闭窗口，webview.start() 返回后恢复
独立登录进程；Tkinter 主窗口继续响应。

页面注入的 window.webUser 必须存在且含用户 id。
返回主进程后还会向源站验证身份，不能仅凭 Cookie 的存在判定成功。

Cookie 获取：pywebview 原生 get_cookies()（可拿 HttpOnly）为主，
document.cookie（需求书方案）补充合并。
"""
import json
import logging
import threading
import time

from . import config

logger = logging.getLogger(__name__)

try:
    import webview
except Exception:  # pragma: no cover
    webview = None

# 网易登录态特征 Cookie（任一出现即视为已登录）
LOGIN_MARKERS = ('NTES_SESS', 'NTES_YSESS', 'P_INFO', 'P_OINFO', 'S_INFO')

_WEBUSER_JS = (
    '(function(){try{var u=window.webUser||null;'
    'if(!u||!u.id)return "";'
    'return JSON.stringify({id:u.id,nickName:u.nickName||u.nickname||""});}'
    'catch(e){return "";}})()'
)


def _looks_logged_in(cookies):
    return bool(cookies) and any(k in cookies for k in LOGIN_MARKERS)


class LoginWindow:
    """阻塞式登录窗口。open() 必须在主线程调用。

    返回 dict：
      {'ok': True, 'cookies': {name: value}, 'user': {id, nickName} | None}
      {'ok': False, 'reason': 'canceled' | 'error', 'message': str}
    """

    def open(self):
        if webview is None:
            return {'ok': False, 'reason': 'error',
                    'message': '未安装 pywebview，无法打开登录窗口（pip install pywebview）'}
        holder = {}
        stop = threading.Event()
        last_cookies = {}

        def poll(win):
            try:
                win.events.loaded.wait(30)   # 等页面首次加载完成
            except Exception:
                pass
            if stop.is_set():
                return
            while not stop.is_set():
                if win.events.closed.is_set():   # 用户直接关窗 -> 走到下方兜底
                    break
                try:
                    d = self._read_cookies(win)
                except Exception:
                    d = {}
                if d:
                    last_cookies.update(d)
                user = self._web_user(win)
                if (user or {}).get('id'):
                    holder['result'] = {'ok': True, 'cookies': last_cookies, 'records': self._read_records(win), 'user': user}
                    stop.set()
                    try:
                        win.destroy()            # pywebview 6: window.destroy()
                    except Exception:
                        pass
                    return
                time.sleep(0.6)
            # 用户手动关闭窗口：若其实已登录，仍视为登录成功（关窗兜底）
            if not holder.get('result'):
                user = self._web_user(win)
                if (user or {}).get('id'):
                    holder['result'] = {'ok': True, 'cookies': last_cookies, 'records': self._read_records(win), 'user': user}

        def init(win):
            threading.Thread(target=poll, args=(win,), daemon=True).start()

        try:
            win = webview.create_window(
                '登录中国大学MOOC', config.HOME_URL,
                width=1024, height=720, resizable=True)
            webview.start(init, (win,), gui='edgechromium', private_mode=True)
        except Exception as e:
            logger.exception('登录窗口启动失败')
            holder['result'] = {'ok': False, 'reason': 'error', 'message': str(e)}
        return holder.get('result') or {'ok': False, 'reason': 'canceled', 'cookies': None}

    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_records(win):
        from http.cookies import SimpleCookie
        from email.utils import parsedate_to_datetime
        records = []
        for cookie in win.get_cookies() or []:
            if isinstance(cookie, SimpleCookie):
                for name, value in cookie.items():
                    expires = None
                    try:
                        if value['expires']:
                            expires = int(parsedate_to_datetime(value['expires']).timestamp())
                    except (ValueError, TypeError, OverflowError):
                        pass
                    records.append({'name': name, 'value': value.value,
                                    'domain': value['domain'] or '.icourse163.org',
                                    'path': value['path'] or '/', 'secure': bool(value['secure']),
                                    'expires': expires})
        return records

    @staticmethod
    def _read_cookies(win):
        """原生 Cookie API 为主，document.cookie 补充合并。"""
        from http.cookies import SimpleCookie
        d = {}
        try:
            cookies = win.get_cookies() or []
        except Exception:
            cookies = []
        for c in cookies:
            try:
                if isinstance(c, SimpleCookie):          # pywebview 返回 SimpleCookie
                    for key in c:
                        d[key] = c[key].value
                elif isinstance(c, dict):
                    if c.get('name'):
                        d[str(c['name'])] = str(c.get('value', ''))
                else:
                    name = getattr(c, 'name', None) or getattr(c, 'Name', None)
                    value = getattr(c, 'value', None) or getattr(c, 'Value', None)
                    if name:
                        d[str(name)] = str(value or '')
            except Exception:
                continue
        try:
            from .cookies import CookieManager
            s = win.evaluate_js('document.cookie') or ''
            for k, v in CookieManager.parse_cookie_header(s).items():
                d.setdefault(k, v)
        except Exception:
            pass
        return d

    @staticmethod
    def _web_user(win):
        """读取页面注入的 window.webUser，返回 {id, nickName} 或 None。"""
        try:
            raw = win.evaluate_js(_WEBUSER_JS) or ''
            if raw.strip():
                return json.loads(raw)
        except Exception:
            pass
        return None
