# -*- coding: utf-8 -*-
"""
通用工具：日志 / 统计 / 轻量加密 / ID 生成
"""

import os
import sys
import json
import time
import re
import uuid
import hashlib
import base64
import threading

from .config import CFG

STATS = {"requests": 0, "failed": 0, "start_time": time.time()}
LOGS = []
LOG_LOCK = threading.Lock()
_LOG_LOCK = LOG_LOCK  # 兼容旧名
MAX_LOGS = int(CFG.get('misc', 'max_logs', 200))


def log(msg):
    with LOG_LOCK:
        ts = time.strftime("%H:%M:%S")
        LOGS.append(f"[{ts}] {msg}")
        if len(LOGS) > MAX_LOGS:
            LOGS.pop(0)
        sys.stderr.write(f"[{ts}] {msg}\n")


# ---------------- debug 模式 ----------------
# 打开后会把每个上游请求的完整内容（URL / 头 / 体 / 响应）记下来。
# 排查"为什么没回复""上游到底回了什么"这类问题时最有用。
#
# ★ 必须脱敏：请求头里带 Cookie(serviceToken / ph)，响应里可能带
#   用户手机号、邮箱、文件签名 URL。原样打出来等于把账号和密码
#   写进日志文件，所以这里统一替换成 ***。
import re as _re

DEBUG = bool(CFG.get("misc", "debug", False))
DEBUG_FILE = CFG.get("misc", "debug_file", "") or ""
DEBUG_MAX = int(CFG.get("misc", "debug_max_body", 8000))
_LOCK2 = threading.Lock()

# 需要打码的字段名（不区分大小写）
_SECRET_KEYS = (
    "servicetoken", "xiaomichatbot_servicetoken", "xiaomichatbot_ph",
    "pass", "password", "pwd", "token", "authorization", "cookie",
    "set-cookie", "secret", "apikey", "api_key", "x-api-key",
    "signature", "galaxyaccesskeyid", "accesskeyid", "accesskeysecret",
)

# URL 里的签名参数
_URL_SECRET = _re.compile(
    r"((?:Signature|GalaxyAccessKeyId|Expires|AccessKeyId|AccessKeySecret|"
    r"sign|token|ticket)=)[^&\s\"']+", _re.I)

# 常见敏感值形态（JWT / 长 base64 / 手机号 / 邮箱）
_VAL_PATTERNS = [
    (_re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"), "***JWT***"),
    (_re.compile(r"/vj[A-Za-z0-9+/=]{20,}"), "***SERVICETOKEN***"),
    (_re.compile(r"\b1[3-9]\d{9}\b"), "***PHONE***"),
    (_re.compile(r"xiaomichatbot_ph\s*=\s*\"?[^;\"\s]+"), "xiaomichatbot_ph=***PH***"),
    (_re.compile(r"[A-Za-z0-9+/]{16,}={0,2}(?=\s*[;\"])"), "***B64***"),
    (_re.compile(r"\b[\w.\-]+@[\w\-]+\.[\w.]+\b"), "***EMAIL***"),
]


def redact_text(s, limit=None):
    """把文本里的敏感信息替换掉"""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    for pat, rep in _VAL_PATTERNS:
        s = pat.sub(rep, s)
    s = _URL_SECRET.sub(lambda m: m.group(1) + "***", s)
    if limit and len(s) > limit:
        s = s[:limit] + f"...<截断, 共{len(s)}字符>"
    return s


def redact_headers(h):
    """请求/响应头脱敏"""
    if not h:
        return {}
    out = {}
    for k, v in (h.items() if hasattr(h, "items") else h):
        lk = str(k).lower()
        if any(x in lk for x in _SECRET_KEYS):
            out[k] = "***"
        else:
            out[k] = redact_text(v, 300)
    return out


def redact_body(obj, limit=None):
    """请求/响应体脱敏：dict 按 key 打码，字符串整体扫一遍"""
    lim = limit if limit is not None else DEBUG_MAX
    if obj is None:
        return None
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} 字节二进制>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(x in lk for x in _SECRET_KEYS):
                out[k] = "***"
            else:
                out[k] = redact_body(v, lim)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_body(x, lim) for x in obj[:20]]
    if isinstance(obj, str):
        # 看起来像 JSON 就解析后再脱敏，否则当普通文本
        t = obj.strip()
        if t.startswith("{") or t.startswith("["):
            try:
                return redact_body(json.loads(t), lim)
            except Exception:
                pass
        return redact_text(obj, lim)
    return obj


def dbg(msg):
    """debug 日志：同时进内存 LOGS 与（可选）日志文件"""
    if not DEBUG:
        return
    with _LOCK2:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        try:
            with LOG_LOCK:
                LOGS.append(line)
                if len(LOGS) > MAX_LOGS:
                    LOGS.pop(0)
        except Exception:
            pass
        sys.stderr.write(line + "\n")
        if DEBUG_FILE:
            try:
                d = os.path.dirname(os.path.abspath(DEBUG_FILE))
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(DEBUG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
            except Exception:
                pass


def dbg_request(method, url, headers=None, body=None, tag=""):
    """记录一个完整的上游请求"""
    if not DEBUG:
        return
    try:
        parts = [f"──▶ {tag or ''} {method} {redact_text(url, 500)}"]
        if headers:
            rh = redact_headers(headers)
            parts.append("    请求头: " + json.dumps(rh, ensure_ascii=False)[:1200])
        if body is not None:
            parts.append("    请求体: " + json.dumps(
                redact_body(body), ensure_ascii=False)[:DEBUG_MAX])
        dbg("\n".join(parts))
    except Exception as e:
        dbg(f"──▶ {tag} {method} (记录请求失败: {e})")


def dbg_response(resp, tag="", body=None, maxlen=None):
    """记录一个完整的上游响应"""
    if not DEBUG:
        return
    try:
        lim = maxlen or DEBUG_MAX
        st = getattr(resp, "status_code", "?")
        parts = [f"◀── {tag or ''} HTTP {st}"]
        try:
            parts.append("    响应头: " + json.dumps(
                redact_headers(resp.headers), ensure_ascii=False)[:800])
        except Exception:
            pass
        if body is None:
            try:
                body = resp_text(resp)
            except Exception:
                body = ""
        if body:
            parts.append("    响应体: " + redact_text(body, lim))
        dbg("\n".join(parts))
    except Exception as e:
        dbg(f"◀── {tag} (记录响应失败: {e})")


def dbg_error(tag, e):
    """记录异常"""
    if not DEBUG:
        return
    dbg(f"✖ {tag} 异常: {type(e).__name__}: {redact_text(str(e), 600)}")


# ---------------- 账号持久化（本地加密落盘） ----------------
# 网页添加的账号会存到这个文件，重启后自动加载。
def accounts_file():
    """账号文件路径（来自 config.json）"""
    return CFG.accounts_file


def secret_file():
    """.secret_key 路径（与账号文件同目录）"""
    return os.path.join(os.path.dirname(os.path.abspath(accounts_file())) or ".",
                        ".secret_key")


def io_open(p):
    """统一以 UTF-8 打开文本文件"""
    return open(p, encoding="utf-8")


def _load_secret():
    """读取或生成 .secret_key"""
    if os.path.exists(secret_file()):
        try:
            return io_open(secret_file()).read().strip()
        except Exception:
            pass
    k = hashlib.sha256(f"{time.time()}{uuid.uuid4()}".encode()).hexdigest()
    try:
        with open(secret_file(), "w", encoding="utf-8") as f:
            f.write(k)
        os.chmod(secret_file(), 0o600)
    except Exception:
        pass
    return k


_SECRET = _load_secret()


def _keystream(salt, n):
    key = hashlib.sha256(_SECRET.encode()).digest()
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(key + salt + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def _enc(s):
    """轻量流加密，避免密码/token 明文落盘（防意外泄露，非强加密）"""
    if not s:
        return ""
    raw = str(s).encode("utf-8")
    salt = os.urandom(8)
    out = bytes(a ^ b for a, b in zip(raw, _keystream(salt, len(raw))))
    return "enc1:" + base64.b64encode(salt + out).decode()


def _dec(s):
    if not s:
        return ""
    if not str(s).startswith("enc1:"):
        return str(s)  # 兼容旧明文
    try:
        blob = base64.b64decode(str(s)[5:])
        salt, out = blob[:8], blob[8:]
        return bytes(a ^ b for a, b in zip(out, _keystream(salt, len(out)))) \
            .decode("utf-8", "replace")
    except Exception:
        return ""


def _force_utf8(r):
    """强制 requests.Response 按 UTF-8 解码。

    requests 对 text/* 且响应头未带 charset 的情况，按 HTTP 规范回退
    ISO-8859-1，导致中文变成 'ä½ å¥½' 这类乱码。
    小米的 text/event-stream 与登录响应都是如此，统一纠偏。

    只在"未声明 / 声明为单字节编码"时接管，不误伤真正的 latin-1 响应。
    """
    try:
        enc = (getattr(r, "encoding", "") or "").lower()
        if not enc or enc in ("iso-8859-1", "latin-1", "latin1", "iso8859-1",
                              "us-ascii", "ascii", "none"):
            r.encoding = "utf-8"
    except Exception:
        pass
    return r


def resp_text(r):
    """安全取响应文本，强制 UTF-8"""
    _force_utf8(r)
    return r.text


def _norm_user(s):
    """归一化用户名：去空格、转小写"""
    return (s or "").strip().lower()


# Cookie 值允许出现的字符（RFC 6265 cookie-octet 的安全子集）。
# serviceToken 是 base64 类字符串，只可能含 A-Za-z0-9+/=._- 和少量符号。
_COOKIE_SAFE = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "+/=_.-%"
)


def sanitize_cookie_value(v):
    """把 Cookie 值里的非法字符去掉

    出现非 ASCII / 控制符通常意味着 token 是坏的：
      - 从网页复制时被截断
      - 跨机器导入但 .secret_key 不匹配，解密出乱码
      - 加密文件损坏

    直接塞进 header 会让 requests/urllib3 抛
    "Invalid leading whitespace, reserved character(s)..."，
    并把整串二进制打到日志里。这里先净化，坏字符记数。
    """
    if v is None:
        return "", 0
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8", "replace")
        except Exception:
            v = v.decode("latin-1", "replace")
    v = str(v)
    bad = [ch for ch in v if ch not in _COOKIE_SAFE]
    if not bad:
        return v, 0
    return "".join(ch for ch in v if ch in _COOKIE_SAFE), len(bad)


def has_binary(s):
    """判断字符串里是否含二进制/控制字符

    只统计真正的不可打印字符（控制符、非 ASCII）。
    空格、标点、换行都算**正常**，不算"非法"。

    之前 _clean_exc 直接拿 sanitize_cookie_value 去洗异常信息，
    而 Cookie 白名单里不含空格，于是
    "list index out of range" 的 4 个空格被当成"凭证非法字符"，
    把普通的 IndexError 伪装成"凭证坏了，请重新导入"，
    真实原因被彻底掩盖。这里改用本函数判断。
    """
    if s is None:
        return 0
    s = str(s)
    n = 0
    for ch in s:
        o = ord(ch)
        # 控制符（换行 / 制表 / 回车除外）
        if (o < 32 and ch not in "\n\t\r") or o == 127:
            n += 1
        # Latin-1 高位区(0x80-0xFF)：解密失败产生的典型乱码
        # 中文 / emoji 在 0x100 以上，属正常可打印字符，不能算非法，
        # 否则 "凭证失效(401)" 这类中文错误也会被误报。
        elif 0x80 <= o <= 0xFF:
            n += 1
    return n


def is_cookie_safe(v):
    """判断 Cookie 值是否干净（用于提前判定账号可用）"""
    _, n = sanitize_cookie_value(v)
    return n == 0


def gen_msg_id():
    return hashlib.md5(f"{time.time()}{uuid.uuid4()}".encode()).hexdigest()


def gen_conv_id():
    return hashlib.md5(f"conv_{time.time()}_{uuid.uuid4()}".encode()).hexdigest()


def _strip_js_prefix(txt):
    """小米登录接口返回的是 `var ... = {...};`, 需要剥掉前缀"""
    txt = txt.strip()
    m = re.search(r"(\{.*\})\s*;?\s*$", txt, re.S)
    if m:
        return m.group(1)
    return txt

# ============================================================
# 会话复用（chat 多轮上下文）
# ============================================================
# OpenAI 协议没有标准的会话 ID 字段，所以用以下优先级确定会话键：
#   1) 请求头 X-Mimo-Conversation-Id（客户端显式指定）
#   2) 请求体 user 字段（OpenAI 标准字段，很多客户端会传）
#   3) 都没有 -> 不复用，每次新建会话
#
# 复用会话后上游能记住上文；但为保险起见默认仍会把历史拼进 query，
# 两条路都不通才可能丢上下文。

class ConversationStore:
    """带 TTL 的会话 ID 缓存。线程安全。"""

    def __init__(self, ttl=1800, max_size=500):
        self.ttl = ttl
        self.max_size = max_size
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key):
        if not key:
            return None
        with self._lock:
            item = self._d.get(key)
            if not item:
                return None
            if time.time() - item["ts"] > self.ttl:
                self._d.pop(key, None)
                return None
            # 续期
            item["ts"] = time.time()
            return item["conv"]

    def put(self, key, conv):
        if not key or not conv:
            return
        with self._lock:
            # 超容量时清掉最旧的一半，避免无上限增长
            if len(self._d) >= self.max_size:
                for k in sorted(self._d, key=lambda k: self._d[k]["ts"])[
                        : max(1, self.max_size // 2)]:
                    self._d.pop(k, None)
            self._d[key] = {"conv": conv, "ts": time.time()}

    def drop(self, key):
        if not key:
            return
        with self._lock:
            self._d.pop(key, None)

    def __len__(self):
        with self._lock:
            return len(self._d)


CONVERSATIONS = ConversationStore()
