#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号文件转换工具
============================================================
把 accounts.json（代理自动保存的加密格式）转成其他可直接导入的形式。

⚠️ 必须在有 .secret_key 的那台机器上运行，否则解不开密文。

用法:
  python account_tool.py decode   [accounts.json]   查看明文（脱敏）
  python account_tool.py config   [accounts.json]   生成 config.json 的 accounts.list 片段
  python account_tool.py cookie   [accounts.json]   生成可粘贴到面板的 Cookie 串
  python account_tool.py encode   [config.json]     反向: config.json -> accounts.json（加密）
  python account_tool.py check    [accounts.json]   检查密钥是否匹配（能否解开）
"""

import os
import sys
import json
import hashlib
import base64


# ---------------- 加解密（与 mimo_proxy/utils.py 完全一致） ----------------

def find_secret(near=None):
    """查找 .secret_key"""
    cands = []
    if near:
        cands.append(os.path.join(os.path.dirname(os.path.abspath(near)), ".secret_key"))
    cands.append(os.path.join(os.getcwd(), ".secret_key"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key"))
    for c in cands:
        if os.path.exists(c):
            try:
                return open(c, encoding="utf-8").read().strip()
            except Exception:
                pass
    return None


def _keystream(secret, salt, n):
    key = hashlib.sha256(secret.encode()).digest()
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(key + salt + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def dec(secret, s):
    if not s:
        return ""
    if not str(s).startswith("enc1:"):
        return str(s)
    try:
        blob = base64.b64decode(str(s)[5:])
        salt, out = blob[:8], blob[8:]
        return bytes(a ^ b for a, b in zip(out, _keystream(secret, salt, len(out)))) \
            .decode("utf-8", "replace")
    except Exception:
        return ""


def enc(secret, s):
    if not s:
        return ""
    raw = str(s).encode("utf-8")
    salt = os.urandom(8)
    out = bytes(a ^ b for a, b in zip(raw, _keystream(secret, salt, len(raw))))
    return "enc1:" + base64.b64encode(salt + out).decode()


def mask(s, n=14):
    if not s:
        return "(空)"
    return s[:n] + "..." + f"(共{len(s)}字符)"


# ---------------- 子命令 ----------------

def load_accounts(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_check(path, secret):
    data = load_accounts(path)
    items = data.get("accounts", [])
    print(f"账号数: {len(items)}")
    ok = 0
    for i, it in enumerate(items):
        ph = dec(secret, it.get("ph_enc", ""))
        tk = dec(secret, it.get("token_enc", ""))
        # 解不开时会产生大量替换字符，用它做判据
        good = bool(tk) and "\ufffd" not in tk and len(tk) > 8
        ok += 1 if good else 0
        print(f"  [{i}] uid={it.get('uid')}  ph={'✅' if ph else '❌'} "
              f"token={'✅' if good else '❌ 解不开'}  {mask(tk)}")
    print(f"\n可解密: {ok}/{len(items)}")
    if ok == 0:
        print("❌ 全部解不开 —— .secret_key 与加密时用的不是同一个")


def cmd_decode(path, secret):
    data = load_accounts(path)
    for i, it in enumerate(data.get("accounts", [])):
        print(f"\n--- 账号 [{i}] ---")
        print(f"  uid   : {it.get('uid')}")
        print(f"  user  : {it.get('user') or '(无)'}")
        print(f"  ph    : {dec(secret, it.get('ph_enc', ''))}")
        print(f"  token : {dec(secret, it.get('token_enc', ''))}")
        pw = dec(secret, it.get("pass_enc", ""))
        if pw:
            print(f"  pass  : {'*' * len(pw)}")


def cmd_config(path, secret):
    """输出可直接粘进 config.json 的 accounts.list"""
    data = load_accounts(path)
    out = []
    for it in data.get("accounts", []):
        ph = dec(secret, it.get("ph_enc", ""))
        tk = dec(secret, it.get("token_enc", ""))
        uid = str(it.get("uid") or "")
        pw = dec(secret, it.get("pass_enc", ""))
        user = it.get("user")
        if not (ph and tk and uid):
            continue
        if user and pw:
            out.append({"user": user, "pass": pw})
        else:
            out.append({"ph": ph, "token": tk, "uid": uid})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n// 把上面这段填进 config.json 的 accounts.list", file=sys.stderr)
    print(f"// 共 {len(out)} 个账号", file=sys.stderr)


def cmd_cookie(path, secret):
    """输出可粘贴到管理面板的 Cookie 串（每行一个账号）"""
    data = load_accounts(path)
    for it in data.get("accounts", []):
        ph = dec(secret, it.get("ph_enc", ""))
        tk = dec(secret, it.get("token_enc", ""))
        uid = str(it.get("uid") or "")
        if not (ph and tk and uid):
            continue
        print(f'serviceToken={tk}; userId={uid}; xiaomichatbot_ph={ph}')


def cmd_encode(path, secret):
    """反向：config.json 的 accounts.list -> accounts.json（加密）"""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    items = (cfg.get("accounts") or {}).get("list") or []
    out = {"accounts": []}
    for it in items:
        out["accounts"].append({
            "user": it.get("user"),
            "pass_enc": enc(secret, it.get("pass", "")),
            "ph_enc": enc(secret, it.get("ph", "")),
            "token_enc": enc(secret, it.get("token", "")),
            "uid": str(it.get("uid") or ""),
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "accounts.json"

    if cmd not in ("decode", "config", "cookie", "encode", "check"):
        print(__doc__)
        return 1

    if not os.path.exists(path):
        print(f"❌ 文件不存在: {path}")
        return 1

    secret = find_secret(path if cmd != "encode" else None)
    if not secret:
        print("❌ 找不到 .secret_key")
        print("   请在存放 accounts.json 的目录运行，或把 .secret_key 复制过来")
        return 1

    {"decode": cmd_decode, "config": cmd_config, "cookie": cmd_cookie,
     "encode": cmd_encode, "check": cmd_check}[cmd](path, secret)
    return 0


if __name__ == "__main__":
    sys.exit(main())
