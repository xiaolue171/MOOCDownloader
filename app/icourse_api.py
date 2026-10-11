# -*- coding: utf-8 -*-
"""
icourse163 API 客户端
=====================
真实接口链路（已实测验证，2025-11）：

1. 预热 GET https://www.icourse163.org/  -> 取得 NTESSTUDYSI（即 csrfKey）
2. 课程页 HTML 解析 termId / courseId / webUser.id / serverTimeDiff
3. DWR POST CourseBean.getLastLearnedMocTermDto.dwr（方法名 getMocTermDto）
   -> 章节->小节->单元（视频 contentType=1，课件 contentType=3）
4. 视频：
   sign = MD5(bizId + "1" + timestamp + "88" + "1" + "mooc" + webUserId)
   POST resourceRpcBean.getResourceTokenV2.rpc?csrfKey=..  (表单)
   -> result.videoSignDto {status:0=免费, signature, videoId, duration}
   GET  vod.study.163.com/eds/api/v1/vod/video?videoId=..&signature=..&clientType=1
   -> result.videos[]（mp4/flv 直链 或 hls m3u8）
5. 课件：ResourceTokenV2 的 docSignDto -> vod /api/v1/doc/{docId}/get
   -> sources 中 type=3 的原始 PDF 地址（2026-10-10 与网页调用核对）
"""
import re
import time
import urllib.parse

import requests

from . import config
from .dwr_parser import parse_dwr_response
from .models import Course, Chapter, Lesson, Unit
from .sign import token_encrypt

TIMEOUT = (10, 30)


class ApiError(Exception):
    def __init__(self, message, raw=None):
        super().__init__(message)
        self.raw = raw


class LoginExpired(ApiError):
    """登录态失效 / 未登录。"""


class PaidCourseError(ApiError):
    """付费课程 / 加密内容，本工具仅支持免费公开课。"""


class ResourceUnavailable(ApiError):
    """The source reports an unavailable or unauthorized resource."""


class CourseClient:
    # 网易登录态 Cookie（用于判断"是否携带了登录身份"）
    AUTH_COOKIES = ('NTES_YSESS', 'NTES_SESS', 'P_INFO', 'P_OINFO', 'S_INFO')

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({'User-Agent': config.USER_AGENT,
                                     'Referer': config.REFERER})
        self.csrf = ''
        self.web_user_id = ''
        self.server_time_diff = 0
        self.course_url = ''
        self.notes = []   # 供 UI 展示的提示信息（如"已降级游客模式"）
        self._abort = False   # 登录态失效标记（下载任务生成器据此提前终止）
        self._details = {}
        self.auth_changed = None

    # ------------------------------------------------------------------ #
    # 基础请求
    # ------------------------------------------------------------------ #
    def _get(self, url, **kw):
        kw.setdefault('allow_redirects', False)
        kw.setdefault('timeout', TIMEOUT)
        try:
            r = self.session.get(url, **kw)
        except requests.exceptions.RequestException as e:
            raise ApiError('网络请求失败: %s' % e) from e
        if r.status_code == 401:
            raise LoginExpired('请求被重定向/拒绝(%s)，登录态可能已失效，请重新登录' % r.status_code)
        if r.status_code != 200:
            raise ApiError('HTTP %s: %s' % (r.status_code, url))
        return r

    def _post(self, url, data=None, json=None, extra_headers=None):
        headers = dict(extra_headers or {})
        try:
            r = self.session.post(url, data=data, json=json, headers=headers,
                                  allow_redirects=False, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise ApiError('网络请求失败: %s' % e) from e
        if r.status_code == 401:
            raise LoginExpired('请求被重定向/拒绝(%s)，登录态可能已失效，请重新登录' % r.status_code)
        if r.status_code != 200:
            raise ApiError('HTTP %s: %s' % (r.status_code, url))
        return r

    def _get_page(self, url):
        """抓取页面（允许重定向，返回最终响应文本）。"""
        try:
            r = self.session.get(url, allow_redirects=True, timeout=TIMEOUT,
                                 headers={'Referer': config.REFERER})
        except requests.exceptions.RequestException as e:
            raise ApiError('网络请求失败: %s' % e) from e
        if r.status_code != 200:
            raise ApiError('HTTP %s: %s' % (r.status_code, url))
        return r.text

    def warmup(self):
        """访问首页，确保会话拿到 NTESSTUDYSI（csrfKey）。仅首次执行。
        首页可能 302 跳转（http->https 等），跟随即可，不算登录失效。"""
        if not self.csrf:
            try:
                r = self.session.get(config.HOME_URL, allow_redirects=True,
                                     timeout=TIMEOUT,
                                     headers={'Referer': config.REFERER})
            except requests.exceptions.RequestException as e:
                raise ApiError('网络请求失败: %s' % e) from e
            if r.status_code not in (200, 301, 302, 303):
                raise ApiError('HTTP %s: %s' % (r.status_code, config.HOME_URL))
            self.csrf = self.session.cookies.get('NTESSTUDYSI') or ''
        return self.csrf

    # ------------------------------------------------------------------ #
    # URL 解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_course_url(url):
        """从课程链接提取 (courseId, termId)。支持：
        /course/100123456
        /course/PKU-100123456
        /learn/100123456?tid=987654
        /learn/PKU-100123456?tid=987654
        """
        url = url.strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('www.icourse163.org', 'icourse163.org'):
            raise ApiError('请输入中国大学 MOOC 的课程或学习链接')
        m = re.search(r'/(?:course|learn)/(?:[A-Za-z0-9_-]+-)?(\d+)', url)
        if not m:
            raise ApiError('无法从链接中识别课程ID，请粘贴 icourse163.org 的课程/学习链接')
        course_id = int(m.group(1))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        term_id = int(q['tid'][0]) if q.get('tid') and q['tid'][0].isdigit() else 0
        return course_id, term_id

    # ------------------------------------------------------------------ #
    # DWR 调用
    # ------------------------------------------------------------------ #
    def _dwr(self, method, params):
        data = {
            'callCount': 1,
            'scriptSessionId': '${scriptSessionId}190',
            'httpSessionId': '',
            'c0-scriptName': 'CourseBean',
            'c0-methodName': method,
            'c0-id': 0,
            'batchId': str(int(time.time() * 1000)),
        }
        for idx, p in enumerate(params):
            data['c0-param%d' % idx] = p
        r = self._post(config.DWR_ENDPOINT, data=data)
        text = r.text
        if '_remoteHandleException' in text:
            import json
            match = re.search(r'message:("(?:\\.|[^"\\])*")', text)
            try:
                message = json.loads(match[1]) if match else '源站暂时无法提供该资源'
            except ValueError:
                message = '源站暂时无法提供该资源'
            raise ResourceUnavailable('源站提示：%s。请确认网页端可访问此资源，必要时登录后重试。' % message)
        try:
            obj = parse_dwr_response(text)
        except Exception as e:
            raise ApiError('DWR 响应解析失败(%s)：%s\n原始返回(前500字)：%s'
                           % (method, e, text[:500])) from e
        if obj is None:
            raise ApiError('DWR 返回为空(%s)\n原始返回(前500字)：%s' % (method, text[:500]))
        return obj

    # ------------------------------------------------------------------ #
    # 课程结构
    # ------------------------------------------------------------------ #
    def load_course(self, url, term_id=0):
        """解析课程结构：课程名 + 章节/小节/单元。"""
        self.warmup()
        course_id, tid_from_url = self.parse_course_url(url)
        term_id = term_id or tid_from_url
        self.course_url = url

        # 抓用户粘贴的原始课程页：拿 termId（若未知）+ webUser.id + serverTimeDiff
        page = self._get_page(url)
        self.course_url = url

        m_tid = re.search(r'termId\s*[=:]\s*"?(\d+)"?', page)
        if m_tid and not term_id:
            term_id = int(m_tid.group(1))
        self.web_user_id = ''
        m_user = re.search(r'window\.webUser\s*=\s*({[^;]+})', page)
        if m_user:
            try:
                import json
                wu = json.loads(m_user.group(1).replace("'", '"'))
                self.web_user_id = str(wu.get('id', ''))
            except Exception:
                pass
        m_diff = re.search(r'serverTimeDiff\s*[=:]\s*(-?\d+)', page)
        if m_diff:
            self.server_time_diff = int(m_diff.group(1))

        if not term_id:
            raise ApiError('未能从课程页解析到 termId（可能课程不存在或已下架）')

        term = self._dwr('getMocTermDto',
                         ['number:%d' % term_id, 'number:0', 'boolean:true'])

        course = Course(course_id=course_id, term_id=term_id)
        course.name = (term.get('name') or term.get('courseName') or '').strip() \
                      or ('课程%d' % course_id)

        for ci, ch in enumerate(term.get('chapters') or [], 1):
            if not ch:
                continue
            chapter = Chapter(chapter_id=ch.get('id') or 0,
                              name=(ch.get('name') or '').strip())
            for li, les in enumerate(ch.get('lessons') or [], 1):
                if not les:
                    continue
                lesson = Lesson(lesson_id=les.get('id') or 0,
                                name=(les.get('name') or '').strip())
                for ui, u in enumerate(les.get('units') or [], 1):
                    if not u:
                        continue
                    ct = u.get('contentType')
                    if ct not in (config.CT_VIDEO, config.CT_DOC):
                        continue  # 跳过测试等
                    unit = Unit(
                        unit_id=u.get('id') or 0,
                        name=(u.get('name') or lesson.name or '').strip(),
                        content_type=ct,
                        content_id=u.get('contentId') or 0,
                        lesson_name=lesson.name,
                        chapter_name=chapter.name,
                        chapter_index=ci, lesson_index=li, unit_index=ui,
                    )
                    lesson.units.append(unit)
                if lesson.units:
                    chapter.lessons.append(lesson)
            if chapter.lessons:
                course.chapters.append(chapter)
        return course

    # ------------------------------------------------------------------ #
    # 单元详情（时长 / 课件链接）
    # ------------------------------------------------------------------ #
    def get_unit_detail(self, unit):
        """getLessonUnitLearnVo：返回单元详情 dict（含 duration/textUrl 等）。"""
        cached = self._details.get(unit.unit_id)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        self.warmup()
        result = self._dwr('getLessonUnitLearnVo',
                         ['number:%d' % unit.content_id,
                          'number:%d' % unit.content_type,
                          'number:0',
                          'number:%d' % unit.unit_id])
        self._details[unit.unit_id] = (time.monotonic(), result)
        return result

    def resolve_duration(self, unit):
        """补齐视频单元时长（失败不抛出）。"""
        try:
            d = self.get_unit_detail(unit)
            vo = d.get('videoVo') or {}
            dur = (d.get('duration') or vo.get('duration')
                   or d.get('videoLearnTime') or 0)
            unit.duration = float(dur or 0)
        except Exception:
            pass

    def resolve_doc(self, unit):
        """Follow the web PDF reader's signed document API, not the retired DWR route."""
        result = self._resource_token(unit)
        doc = result.get('docSignDto') or {}
        if doc.get('status') != 'SUCCESS':
            raise ResourceUnavailable('课件暂不可用（源站状态：%s），请确认网页端可以打开此课件。'
                                      % (doc.get('status') or '未返回文档授权'))
        doc_id, signature = doc.get('docId'), doc.get('signature')
        if not str(doc_id).isdigit() or not signature:
            raise ApiError('课件接口未返回完整的文档授权，请稍后重试')
        response = self._get(config.DOC_API.format(doc_id=doc_id),
                             params={'signature': signature, 'clientType': 1},
                             headers={'Referer': config.REFERER})
        try:
            payload = response.json()
        except ValueError as error:
            raise ApiError('课件地址接口返回格式异常，请稍后重试') from error
        if payload.get('code') == -2:
            raise ApiError('课件下载授权已过期，正在重新获取；若仍失败请重试任务')
        if payload.get('code') != 0:
            raise ApiError('课件地址获取失败（源站代码：%s）' % payload.get('code'))
        detail = payload.get('result') or {}
        # Other entries may be encrypted PDFs, page images or Flash files. They
        # must never be written as if they were a normal PDF.
        source = next((s for s in detail.get('sources') or []
                       if s.get('type') == 3 and s.get('url')), None)
        if not source:
            raise ResourceUnavailable('源站没有提供可下载的 PDF 文件，当前仅提供加密内容或预览格式')
        url = source['url']
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ApiError('源站返回了无效的课件文件地址')
        # The web reader upgrades these legacy NOS links on its HTTPS page.
        if parsed.scheme == 'http' and any(parsed.hostname == domain or parsed.hostname.endswith('.'+domain)
                                           for domain in ('netease.com', '163yun.com', '127.net')):
            parsed = parsed._replace(scheme='https')
            url = urllib.parse.urlunparse(parsed)
        q = urllib.parse.parse_qs(parsed.query)
        fname = (q.get('download') or [None])[0] or detail.get('fileName') or doc.get('fileName')
        fname = fname or urllib.parse.unquote(parsed.path.rsplit('/', 1)[-1])
        if not fname.lower().endswith(('.pdf', '.ppt', '.pptx', '.doc', '.docx')):
            fname += '.pdf'
        return url, fname

    def _resource_token(self, unit):
        self.warmup()
        ts = int(time.time() * 1000) - self.server_time_diff
        sign = self.token_encrypt(unit.unit_id, unit.content_type, ts, self.web_user_id)
        response = self._post(config.TOKEN_RPC + '?csrfKey=' + self.csrf,
                              data={'bizId': unit.unit_id, 'bizType': 1,
                                    'contentType': unit.content_type, 'sign': sign, 'timestamp': ts},
                              extra_headers={'csrfKey': self.csrf})
        try:
            payload = response.json()
        except ValueError as error:
            raise ApiError('资源授权接口返回格式异常，请稍后重试') from error
        result = payload.get('result') or {}
        if payload.get('code') != 0 or result.get('code', 0) != 0:
            raise ResourceUnavailable('源站未授权此资源（代码：%s），请确认网页端访问权限后重试。'
                                      % (payload.get('code') or result.get('code')))
        return result

    # ------------------------------------------------------------------ #
    # 视频地址
    # ------------------------------------------------------------------ #
    @staticmethod
    def token_encrypt(biz_id, content_type, timestamp, web_user_id=''):
        return token_encrypt(biz_id, content_type, timestamp, web_user_id)

    def get_video_urls(self, unit, quality_label):
        """取视频播放地址列表并按清晰度选一个，返回 (url, fmt, label, duration)。

        免费课无需登录：当携带的登录态失效（例如网页端与应用内同时登录同一
        账号、会话被顶掉）时，自动清空登录 Cookie 降级为游客模式重试一次。
        付费/加密课程抛 PaidCourseError。
        """
        try:
            return self._resolve_video(unit, quality_label)
        except LoginExpired:
            if self._has_auth_cookies():
                self.notes.append(
                    '检测到登录态异常（可能被网页端登录顶掉），已降级为游客模式重试，'
                    '免费课不受影响')
                self._drop_auth()
                return self._resolve_video(unit, quality_label)
            raise

    def _has_auth_cookies(self):
        return any(self.session.cookies.get(k) for k in self.AUTH_COOKIES)

    def _drop_auth(self):
        """清空会话 Cookie，重新以游客身份取 csrfKey。"""
        self.session.cookies.clear()
        self.web_user_id = ''
        self.csrf = ''
        if self.auth_changed:
            self.auth_changed()
        try:
            self.warmup()
        except Exception:
            pass

    def _resolve_video(self, unit, quality_label):
        self.warmup()
        csrf = self.csrf or self.warmup()
        ts = int(time.time() * 1000) - self.server_time_diff
        sign = self.token_encrypt(unit.unit_id, unit.content_type, ts, self.web_user_id)

        r = self._post(config.TOKEN_RPC + '?csrfKey=' + csrf,
                       data={'bizId': unit.unit_id, 'bizType': 1,
                             'contentType': unit.content_type,
                             'sign': sign, 'timestamp': ts},
                       extra_headers={'csrfKey': csrf})
        try:
            rpc = r.json()
        except ValueError:
            raise ApiError('Token 接口返回非JSON\n原始返回：%s' % r.text[:500])
        result = rpc.get('result') or {}
        vsd = result.get('videoSignDto') or {}
        status = vsd.get('status')
        if status not in (0, None):
            raise PaidCourseError(
                '当前视频需要付费或未授权（token状态=%s），本工具仅支持免费公开课' % status,
                raw=rpc)
        sig = result.get('signature') or vsd.get('signature')
        if not sig:
            raise ApiError('Token 接口未返回签名\n原始返回：%s' % r.text[:500], raw=rpc)

        vod = self._get(config.VOD_API,
                        params={'videoId': unit.content_id, 'signature': sig,
                                'clientType': 1},
                        headers={'Referer': config.REFERER}).json()
        if vod.get('code') != 0:
            raise ApiError('视频地址接口失败: %s' % vod.get('message') or vod)
        res = vod.get('result') or {}
        items = res.get('videos') or []
        usable = [it for it in items
                  if it.get('videoUrl') and it.get('format') in ('hls', 'mp4', 'flv')]
        if any(it.get('k') or it.get('e') is True for it in items):
            raise PaidCourseError('当前视频为加密内容（付费课程），本工具仅支持免费公开课', raw=vod)
        if not usable:
            raise ApiError('未找到可下载的视频地址\n原始返回：%s' % str(vod)[:500])

        url, fmt, label = self._pick_quality(usable, quality_label)
        duration = float(res.get('duration') or 0)
        return url, fmt, label, duration

    @staticmethod
    def _pick_quality(items, quality_label):
        """Use dimensions when available; never present a source tier as pixels."""
        want = config.QUALITY_RANK.get(quality_label, 2)
        def info(item):
            height = item.get('height') or item.get('videoHeight') or 0
            try:
                height = int(height)
            except (TypeError, ValueError):
                height = 0
            resolution = str(item.get('resolution') or '')
            match = re.search(r'\d+\s*[xX*]\s*(\d+)', resolution)
            if not height and match:
                height = int(match[1])
            if height:
                return (1 if height <= 480 else 2 if height <= 720 else 3, height, '%dP' % height)
            path = urllib.parse.urlparse(item['videoUrl']).path.lower()
            for rank, token, label in ((3, 'shd', '超清'), (2, 'hd', '高清'), (1, 'sd', '标清')):
                if re.search(r'(?:^|[/_.-])' + token + r'(?:[_.\-/]|$)', path):
                    return rank, 0, label + '（源站档位）'
            match = re.search(r'(?:^|[/_.-])(360|480|540|720|1080|2160)p?(?:[_.\-/]|$)', path)
            if match:
                h = int(match[1])
                return (1 if h <= 480 else 2 if h <= 720 else 3, h, '%dP' % h)
            return 0, 0, '源站默认（分辨率未知）'
        ranked = [(info(it), it) for it in items]
        recognized = [(meta, it) for meta, it in ranked if meta[0]]
        if recognized:
            eligible = [(meta, it) for meta, it in recognized if meta[0] <= want]
            if not eligible:
                minimum = min(meta[0] for meta, _ in recognized)
                eligible = [(meta, it) for meta, it in recognized if meta[0] == minimum]
            meta, best = max(eligible, key=lambda pair: (pair[0][0], pair[0][1], pair[1].get('format') == 'mp4'))
        else:
            meta, best = max(ranked, key=lambda pair: pair[1].get('format') == 'mp4')
        return best['videoUrl'], best.get('format', 'hls'), meta[2]

    def verify_identity(self):
        import json
        page = self._get_page(config.HOME_URL)
        match = re.search(r'window\.webUser\s*=\s*({[^;]+})', page)
        try:
            user = json.loads(match.group(1)) if match else {}
        except (ValueError, AttributeError):
            user = {}
        if not user or not user.get('id'):
            raise LoginExpired('当前登录信息未通过验证，请重新登录')
        self.web_user_id = str(user['id'])
        return {'id': self.web_user_id, 'nickName': user.get('nickName') or user.get('nickname') or '已登录用户'}
