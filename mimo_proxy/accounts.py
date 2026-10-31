# -*- coding: utf-8 -*-
"""
账号与账号池：登录、轮询、故障转移、加密持久化
"""

import os
import sys
import json
import time
import re
import hashlib
import threading

import requests

from .config import CFG
from .utils import (log, accounts_file, _enc, _dec, _strip_js_prefix,
                    _norm_user, io_open, _force_utf8, resp_text,
                    gen_msg_id, gen_conv_id)

class Account:
    """一个 MiMo 账号, 支持 user/pass 自动登录, 也支持直接给 token"""

    def __init__(self, **kw):
        self.user = kw.get("user")
        # 兼容 {"pass":...}（环境变量/API）与 {"password":...}（内部调用）
        self.password = kw.get("pass") or kw.get("password")
        self.ph = kw.get("ph", "")
        self.token = kw.get("token", "")
        self.uid = kw.get("uid", "")
        self.session = requests.Session()
        self.lock = threading.Lock()
        self.last_error = None
        self.fail_count = 0

    def cookie_header(self):
        """构造 Cookie 请求头

        token / ph 可能含非 ASCII 或控制符（复制截断、跨机器密钥不匹配、
        加密文件损坏等）。直接拼进 header 会让 urllib3 抛
        "Invalid leading whitespace, reserved character(s)..."，并把整串
        二进制打到日志里。这里先净化，坏字符数记到 last_error 供面板显示。
        """
        from .utils import sanitize_cookie_value
        tok, n_tok = sanitize_cookie_value(self.token)
        ph, n_ph = sanitize_cookie_value(self.ph)
        uid, _ = sanitize_cookie_value(self.uid)
        if n_tok or n_ph:
            self.last_error = (
                f"凭证含 {n_tok + n_ph} 个非法字符已剔除，"
                f"请重新导入 Cookie 或用账密重新登录"
            )
        return (
            f'xiaomichatbot_serviceToken="{tok}"; '
            f'userId={uid}; '
            f'xiaomichatbot_ph="{ph}"'
        )

    def is_valid(self):
        """凭证可用判定

        除了非空，还要检查是否是干净的 Cookie 安全字符 —— 一串二进制
        乱码的 token 一定不是能用凭证，宁可标记失效触发自动重登，
        也别拿去发请求（必然报错）。
        """
        if not (self.token and self.uid and self.ph):
            return False
        try:
            from .utils import is_cookie_safe
            return is_cookie_safe(self.token) and is_cookie_safe(self.ph)
        except Exception:
            return True

    def login(self):
        """用用户名/密码走小米 serviceLoginAuth2 协议拿 serviceToken"""
        if not self.user or not self.password:
            raise RuntimeError(f"账号 {self.user} 没有配置密码, 无法自动登录")
        s = self.session
        from .mimo import HEADERS_TPL
        s.headers.update({"User-Agent": HEADERS_TPL["User-Agent"]})

        # Step 1: 拿签名参数
        r = s.get(
            "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true",
            params={"sid": "xiaomiio", "_json": "true"},
            cookies={"userId": self.user},
            timeout=30,
        )
        r.raise_for_status()
        txt = _strip_js_prefix(resp_text(r))
        try:
            loc = json.loads(txt)
        except Exception:
            raise RuntimeError(
                f"Step1 响应不是合法 JSON（前 120 字符: {txt[:120]!r}），"
                f"可能是登录页改版或被风控拦截"
            )
        if not isinstance(loc, dict):
            raise RuntimeError(f"Step1 响应格式异常: {type(loc).__name__}")
        if "_sign" not in loc:
            raise RuntimeError(
                f"Step1 未返回 _sign（响应: {str(loc)[:200]}），"
                f"登录流程可能已改版"
            )
        sign = loc["_sign"]
        qs = loc.get("qs", "")
        callback = loc.get("callback", "https://sts.api.io.mi.com/sts")

        # Step 2: 提交用户名密码
        pwd_hash = hashlib.md5(self.password.encode("utf-8")).hexdigest().upper()
        data = {
            "sid": "xiaomiio",
            "hash": pwd_hash,
            "callback": callback,
            "qs": qs,
            "user": self.user,
            "_sign": sign,
            "_json": "true",
        }
        r = s.post(
            "https://account.xiaomi.com/pass/serviceLoginAuth2",
            data=data,
            params={"_json": "true"},
            timeout=30,
        )
        r.raise_for_status()
        try:
            auth = json.loads(_strip_js_prefix(resp_text(r)))
        except Exception:
            raise RuntimeError(
                f"Step2 响应不是合法 JSON"
                f"（前 120 字符: {resp_text(r)[:120]!r}），"
                f"可能被风控拦截或需要验证码"
            )
        if not isinstance(auth, dict):
            raise RuntimeError(f"Step2 响应格式异常: {type(auth).__name__}")
        code = auth.get("code")
        if code in (70002, 70016, 81003, 87001):
            raise RuntimeError(f"登录失败 code={code} (密码错误/需要验证码)")
        if code and code != 0:
            raise RuntimeError(f"登录失败 code={code} msg={auth.get('description','')}")

        # Step 3: 访问 location 拿 Set-Cookie: serviceToken
        #
        # auth["location"] 可能是**空字符串**（需要验证码 / 二次验证 /
        # 风控拦截时）。以前直接 s.get("") 会抛 requests 的
        #   "Invalid URL '': No scheme supplied. Perhaps you meant https://?"
        # 完全看不出真实原因。这里先校验，给出可读的中文诊断。
        location = (auth.get("location") or "").strip()
        if not location:
            desc = (auth.get("description") or auth.get("desc")
                    or auth.get("message") or "")
            raise RuntimeError(
                f"登录未完成：响应里没有跳转地址（code={code} {desc}）。"
                f"通常是账号需要验证码或二次验证——请先用浏览器在 "
                f"https://account.xiaomi.com 完整登录一次，"
                f"再从浏览器复制 Cookie 用『Cookie 导入』方式添加。"
            )
        if not (location.startswith("http://")
                or location.startswith("https://")):
            raise RuntimeError(
                f"登录返回的跳转地址不是合法 URL: {location!r}"
            )
        s.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="xiaomi.com")
        s.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="mi.com")
        r = s.get(location, timeout=30, allow_redirects=True)
        r.raise_for_status()

        new_token = r.cookies.get("serviceToken")
        if not new_token:
            # 有些响应直接把 token 放在 body 里
            m = re.search(r'serviceToken["\s:=]+([^"\'}\s,]+)', resp_text(r))
            if m:
                new_token = m.group(1)
        if not new_token:
            raise RuntimeError("未能从登录响应中拿到 serviceToken (可能需要验证码/二次验证)")

        from .utils import sanitize_cookie_value
        clean_tok, bad = sanitize_cookie_value(new_token)
        if bad or not clean_tok:
            raise RuntimeError(
                f"登录返回的 serviceToken 无效（含 {bad} 个非法字符或为空），"
                f"请检查账号状态"
            )
        self.token = clean_tok
        new_uid = auth.get("userId")
        if new_uid:
            self.uid = str(new_uid)
        if not self.uid:
            raise RuntimeError("登录成功但响应里没有 userId，账号不可用")
        # ph 需要从 cookie 或账号信息里拿; 如果登录后没拿到, 保留旧的
        ph_from_cookie = r.cookies.get("xiaomichatbot_ph") or s.cookies.get("xiaomichatbot_ph")
        if ph_from_cookie:
            clean_ph, bad_ph = sanitize_cookie_value(ph_from_cookie)
            if clean_ph and not bad_ph:
                self.ph = clean_ph
        self.fail_count = 0
        self.last_error = None
        return True

    def ensure_valid(self):
        with self.lock:
            if self.is_valid():
                return True
            if self.user and self.password:
                return self.login()
            return False

    def mark_fail(self, err):
        self.fail_count += 1
        self.last_error = str(err)[:200]
        if self.fail_count >= 3:
            # 连续失败 3 次, 清掉 token 触发下次自动刷新
            self.token = ""

    def __repr__(self):
        return f"Account(uid={self.uid}, user={self.user}, valid={self.is_valid()})"


class AccountPool:
    """多账号轮询 + 故障转移"""

    def __init__(self):
        self.accounts = []
        self.idx = 0
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        # 从 config.json 读取账号（不再用环境变量）
        acc_cfg = CFG.get("accounts") or {}
        items = list(acc_cfg.get("list") or [])
        if not items:
            # 单账号快捷写法
            ph = acc_cfg.get("ph", "")
            token = acc_cfg.get("token", "")
            uid = acc_cfg.get("uid", "")
            if ph and token and uid:
                items = [{"ph": ph, "token": token, "uid": uid}]
        for it in items:
            self.accounts.append(Account(**it))
        # 再加载网页添加、已持久化的账号
        self._load_file()

    def _load_file(self):
        """从 accounts.json 恢复账号（敏感字段解密）"""
        if not os.path.exists(accounts_file()):
            return
        try:
            with io_open(accounts_file()) as f:
                data = json.load(f)
        except Exception as e:
            sys.stderr.write(f"[warn] 读取 {accounts_file()} 失败: {e}\n")
            return
        n = 0
        for it in data.get("accounts", []):
            user = it.get("user") or None
            uid = str(it.get("uid") or "")
            # 去重：与环境变量里的账号重复则跳过
            if any((a.user and user and _norm_user(a.user) == _norm_user(user))
                   or (a.uid and uid and str(a.uid) == uid)
                   for a in self.accounts):
                continue
            self.accounts.append(Account(
                user=user,
                password=_dec(it.get("pass_enc", "")) or None,
                ph=_dec(it.get("ph_enc", "")),
                token=_dec(it.get("token_enc", "")),
                uid=uid,
            ))
            n += 1
        if n:
            log(f"从 {os.path.basename(accounts_file())} 载入 {n} 个账号")

    def save(self):
        """持久化当前账号池（敏感字段加密，文件权限 600）"""
        try:
            data = {"accounts": []}
            for a in self.accounts:
                data["accounts"].append({
                    "user": a.user,
                    "pass_enc": _enc(a.password),
                    "ph_enc": _enc(a.ph),
                    "token_enc": _enc(a.token),
                    "uid": a.uid,
                })
            tmp = accounts_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, accounts_file())
            try:
                os.chmod(accounts_file(), 0o600)
            except Exception:
                pass
            return True
        except Exception as e:
            log(f"保存账号失败: {e}")
            return False

    def get_next(self):
        """轮询取下一个可用账号; 若 token 过期且开自动刷新, 尝试重新登录"""
        with self.lock:
            n = len(self.accounts)
            if n == 0:
                return None
            start = self.idx
            for _ in range(n):
                acc = self.accounts[self.idx]
                self.idx = (self.idx + 1) % n
                if acc.is_valid():
                    return acc
                if CFG.auto_refresh and acc.user and acc.password:
                    try:
                        if acc.login():
                            return acc
                    except Exception as e:
                        acc.last_error = str(e)[:200]
            # 一圈都没找到, 返回第一个再试一次
            return self.accounts[start]

    def all(self):
        """返回账号对象列表（内部用，注意不要暴露凭据）"""
        with self.lock:
            return list(self.accounts)

    @staticmethod
    def _redact(text, *secrets):
        """从错误文本里抹掉凭证

        ★ 真问题：requests 的异常信息**包含完整 URL**，而 URL 里带着
          ?xiaomichatbot_ph=xxx。于是 last_error 形如：

            403 Client Error: Forbidden for url: ...react/chat?xiaomichatbot_ph=PH1%3D%3D

          面板直接展示 last_error -> ph 明文泄漏到页面，日志也会打到终端。
          这里统一替换成 ***。
        """
        if not text:
            return text
        out = str(text)
        # 1) 已知凭证整体替换
        for sec in secrets:
            if sec and len(str(sec)) >= 4:
                out = out.replace(str(sec), "***")
        # 2) URL 参数兜底（含 URL 编码形式）
        import re as _re
        import urllib.parse as _up
        pat = r"(?:xiaomichatbot_ph|serviceToken|token|ph)=([^&\s]+)"
        for m in _re.finditer(pat, out):
            val = m.group(1)
            if len(val) >= 4:
                out = out.replace(val, "***")
        # 3) 解码后再比对一次（%3D%3D 这类）
        try:
            dec = _up.unquote(out)
            for m in _re.finditer(pat, dec):
                val = m.group(1)
                if len(val) >= 4:
                    out = out.replace(val, "***")
                    out = out.replace(_up.quote(val, safe=""), "***")
        except Exception:
            pass
        return out


    def status(self):
        # 注意：不返回 password / token / ph 明文
        return [
            {
                "uid": a.uid,
                "user": a.user,
                "valid": a.is_valid(),
                "has_password": bool(a.password),
                "has_ph": bool(a.ph),
                "fail_count": a.fail_count,
                "last_error": AccountPool._redact(
                    a.last_error, a.ph, a.token, a.password),
            }
            for a in self.accounts
        ]

    def add_or_update(self, uid, token, ph):
        """按 uid 导入；已存在则覆盖 token/ph，不重复添加"""
        with self.lock:
            for a in self.accounts:
                if str(a.uid) == str(uid):
                    a.token = token
                    a.ph = ph
                    a.fail_count = 0
                    a.last_error = None
                    target = a
                    break
            else:
                target = Account(uid=uid, token=token, ph=ph)
                self.accounts.append(target)
        self.save()
        return target

    def add_credential(self, user, password, ph=None):
        """按小米账号(邮箱/手机号)+密码添加账号，并立即登录拿 token。
        返回 (account, ok, error)
        """
        with self.lock:
            for a in self.accounts:
                if a.user and _norm_user(a.user) == _norm_user(user):
                    a.password = password
                    if ph:
                        a.ph = ph
                    a.fail_count = 0
                    a.last_error = None
                    target = a
                    break
            else:
                target = Account(user=user, password=password, ph=ph or "")
                self.accounts.append(target)
        # 锁外执行登录（网络请求）
        try:
            ok = target.login()
            err = None
        except Exception as e:
            ok = False
            err = str(e)[:300]
            target.last_error = err
        self.save()
        return target, bool(ok), err

    def remove(self, idx):
        with self.lock:
            if idx < 0 or idx >= len(self.accounts):
                raise IndexError(f"无效索引 {idx}")
            acc = self.accounts.pop(idx)
        self.save()
        return acc

    def test_account(self, acc):
        """发一条最小消息验证账号是否真的可用"""
        payload = {
            "msgId": gen_msg_id(),
            "conversationId": gen_conv_id(),
            "query": "hi",
            "isEditedQuery": False,
            "modelConfig": {"enableThinking": False, "webSearchStatus": "disabled",
                            "model": "mimo-v2.6-pro"},
            "multiMedias": [],
        }
        try:
            # 延迟导入避免模块循环依赖
            from .mimo import call_mimo, parse_sse_events
            raw = call_mimo(acc, payload)
            events = parse_sse_events(raw)
            for evt, data in events:
                if isinstance(data, dict):
                    if data.get("messageType") == "assistant" and data.get("content"):
                        acc.fail_count = 0
                        acc.last_error = None
                        return True
                    # 401 会带 loginUrl
                    if "loginUrl" in str(data) or data.get("code") == 401:
                        acc.mark_fail("凭证失效(401)")
                        return False
            acc.mark_fail("无有效响应")
            return False
        except Exception as e:
            acc.mark_fail(e)
            return False

# 全局账号池
POOL = AccountPool()
