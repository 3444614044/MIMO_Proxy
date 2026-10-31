# -*- coding: utf-8 -*-
"""
管理面板鉴权：独立管理员用户名+密码，会话 Token
"""

import os
import sys
import json
import time
import hashlib
import uuid
import threading

from .config import CFG
from .utils import log, _norm_user

# 会话: token -> {"user":..., "expire":...}
ADMIN_SESSIONS = {}
_ADMIN_SESS_LOCK = threading.Lock()


def _session_ttl():
    """会话有效期（秒），来自 config.json"""
    return int(CFG.get("admin", "session_ttl", 43200))


def admin_accounts():
    """管理员凭据列表（来自 config.json，与小米账号无关）"""
    lst = CFG.get("admin", "accounts") or []
    out = []
    for it in lst:
        if isinstance(it, dict):
            u = it.get("user") or it.get("username") or it.get("email") or ""
            pw = it.get("pass") or it.get("password") or ""
            if u and pw:
                out.append({"user": str(u), "pass": str(pw)})
    if not out:
        out = [{"user": CFG.get("admin", "user", "admin"),
                "pass": CFG.get("admin", "pass", "admin")}]
    return out


def admin_login(username, password):
    """校验管理员用户名+密码，成功签发 session token，失败返回 None"""
    uname_norm = _norm_user(username)
    for a in admin_accounts():
        if _norm_user(a.get("user", "")) == uname_norm \
                and str(a.get("pass", "")) == password:
            tok = hashlib.sha256(
                f"{uname_norm}{time.time()}{uuid.uuid4()}".encode()).hexdigest()
            with _ADMIN_SESS_LOCK:
                # 顺手清理过期会话
                now = time.time()
                for k in [k for k, v in ADMIN_SESSIONS.items() if v["expire"] < now]:
                    ADMIN_SESSIONS.pop(k, None)
                ADMIN_SESSIONS[tok] = {
                    "user": str(a.get("user", "")),
                    "expire": now + _session_ttl(),
                }
            log(f"管理员登录: {a.get('user','')}")
            return tok, _session_ttl()
    log(f"管理员登录失败: {username}")
    return None, 0


def admin_check(token):
    """校验 session token，返回用户名或 None"""
    if not token:
        return None
    with _ADMIN_SESS_LOCK:
        s = ADMIN_SESSIONS.get(token)
        if not s:
            return None
        if s["expire"] < time.time():
            ADMIN_SESSIONS.pop(token, None)
            return None
        return s["user"]


def admin_logout(token):
    with _ADMIN_SESS_LOCK:
        ADMIN_SESSIONS.pop(token, None)