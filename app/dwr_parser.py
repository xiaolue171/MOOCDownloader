# -*- coding: utf-8 -*-
"""
icourse163 DWR 响应解析器
=========================
DWR(3.x) 的响应文本是 JS 赋值语句序列，例如：

    //#DWR-INSERT
    var s1=[];var s3={};
    s3.contentId=null;s3.contentType=1;...
    s1[0]=s3;
    //#DWR-REPLY
    dwr.engine._remoteHandleCallback('1787385789081','0',{chapters:s1,...});

本模块把这类文本还原成 Python 对象（dict/list），不依赖任何 JS 引擎。
已在真实课程上验证通过（见开发记录）。
"""
import json
import re

__all__ = ['parse_dwr_response', 'DwrParseError']


class DwrParseError(ValueError):
    pass


def _split_statements(text):
    """以 ';' 切分语句（跳过字符串内部的 ';'）。"""
    stmts = []
    cur = []
    in_str = False
    esc = False
    for c in text:
        if in_str:
            cur.append(c)
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
                cur.append(c)
            elif c == ';':
                stmts.append(''.join(cur))
                cur = []
            else:
                cur.append(c)
    if ''.join(cur).strip():
        stmts.append(''.join(cur))
    return stmts


class _JsValueParser:
    """解析 DWR 中的 JS 风格值：对象/数组/字符串/数字/布尔/null/引用 sN。"""

    def __init__(self, s, env):
        self.s = s
        self.i = 0
        self.env = env

    def _ws(self):
        while self.i < len(self.s) and self.s[self.i] in ' \t\r\n':
            self.i += 1

    def parse(self):
        self._ws()
        if self.i >= len(self.s):
            raise DwrParseError('unexpected end of DWR value')
        c = self.s[self.i]
        if c == '{':
            return self._obj()
        if c == '[':
            return self._arr()
        if c == '"':
            return self._str()
        if c == 'n':
            self.i += 4
            return None
        if c == 't':
            self.i += 4
            return True
        if c == 'f':
            self.i += 5
            return False
        m = re.match(r'[sS]\d+', self.s[self.i:])
        if m:
            name = m.group(0)
            self.i += len(name)
            key = 's%d' % int(name[1:])
            if key not in self.env:
                raise DwrParseError('unknown DWR ref %s' % key)
            return self.env[key]
        m = re.match(r'-?\d+(\.\d+)?([eE][+-]?\d+)?', self.s[self.i:])
        if m:
            tok = m.group(0)
            self.i += len(tok)
            try:
                return json.loads(tok)
            except Exception:
                return float(tok)
        raise DwrParseError('cannot parse DWR value near: %r' % self.s[self.i:self.i + 40])

    def _obj(self):
        self.i += 1  # {
        obj = {}
        while True:
            self._ws()
            if self.i >= len(self.s):
                raise DwrParseError('unterminated object')
            if self.s[self.i] == '}':
                self.i += 1
                return obj
            if self.s[self.i] == '"':
                k = self._str()
            else:
                m = re.match(r'[A-Za-z_$][\w$]*', self.s[self.i:])
                if not m:
                    raise DwrParseError('bad key near %r' % self.s[self.i:self.i + 20])
                k = m.group(0)
                self.i += len(k)
            self._ws()
            if self.i < len(self.s) and self.s[self.i] == ':':
                self.i += 1
            obj[k] = self.parse()
            self._ws()
            if self.i < len(self.s) and self.s[self.i] == ',':
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == '}':
                self.i += 1
                return obj
            else:
                raise DwrParseError('expected , or } near %r' % self.s[self.i:self.i + 20])

    def _arr(self):
        self.i += 1  # [
        arr = []
        while True:
            self._ws()
            if self.i >= len(self.s):
                raise DwrParseError('unterminated array')
            if self.s[self.i] == ']':
                self.i += 1
                return arr
            arr.append(self.parse())
            self._ws()
            if self.i < len(self.s) and self.s[self.i] == ',':
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == ']':
                self.i += 1
                return arr
            else:
                raise DwrParseError('expected , or ] near %r' % self.s[self.i:self.i + 20])

    def _str(self):
        self.i += 1  # "
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == '\\':
                nxt = self.s[self.i + 1] if self.i + 1 < len(self.s) else ''
                out.append('\\' + nxt)
                self.i += 2
            elif c == '"':
                self.i += 1
                try:
                    return json.loads('"' + ''.join(out) + '"')
                except Exception:
                    return ''.join(out)
            else:
                out.append(c)
                self.i += 1
        raise DwrParseError('unterminated string')


def parse_dwr_response(text):
    """解析 DWR 响应文本，返回 _remoteHandleCallback 的第三参数对象。"""
    env = {}
    result = None
    for st in _split_statements(text):
        # 去掉行注释（//#DWR-INSERT 等可能与声明同行）
        s = '\n'.join(p for p in st.split('\n') if not p.strip().startswith('//'))
        s = s.strip()
        if not s:
            continue
        m = re.match(r'^var (s\d+)=(\[\]|\{\})$', s)
        if m:
            env[m.group(1)] = [] if m.group(2) == '[]' else {}
            continue
        m = re.match(r'^(s\d+)\[(\d+)\]=(.*)$', s)
        if m:
            name, idx, val = m.group(1), int(m.group(2)), m.group(3)
            arr = env[name]
            while len(arr) <= idx:  # JS 稀疏数组：自动扩容
                arr.append(None)
            arr[idx] = _JsValueParser(val, env).parse()
            continue
        m = re.match(r'^(s\d+)\.([A-Za-z_$][\w$]*)=(.*)$', s)
        if m:
            name, field, val = m.group(1), m.group(2), m.group(3)
            env[name][field] = _JsValueParser(val, env).parse()
            continue
        m = re.match(r"^dwr\.engine\._remoteHandleCallback\('([^']*)','([^']*)',", s)
        if m:
            rest = s[m.end():]
            result = _JsValueParser(rest, env).parse()
            continue
        # 其他语句（如 var sN=[] 带初始值等）尽量忽略
    return result
