# -*- coding: utf-8 -*-
"""
视频 Token 签名算法（纯标准库）
================================
站点 JS 逆向所得（videoAesUtilsMin -> tokenEncrypt）：

    sign = MD5(bizId + bizType + timestamp + "88" + contentType + "mooc" + webUserId)
    其中 bizType 固定为 1，mooc 为固定盐，webUserId 未登录时为空串

已用真实抓包样本回归验证（见 tests/test_token.py）。
"""
import hashlib


def token_encrypt(biz_id, content_type, timestamp, web_user_id=''):
    raw = '%s%s%s88%smooc%s' % (biz_id, 1, timestamp, content_type, web_user_id)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()
