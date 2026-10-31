# -*- coding: utf-8 -*-
"""
MiMo 上游客户端：HTTP 请求、上传、chat 调用
"""

import os
import threading
import queue
import sys
import json
import re
import time
import base64
import uuid
import hashlib
import urllib.parse

import requests

from .config import CFG
from .utils import (log, dbg, dbg_request, dbg_response, dbg_error,
                    _force_utf8, resp_text, gen_msg_id, gen_conv_id)
from .tools import (inject_tools_prompt, strip_tool_residue,
                    serialize_tool_turns)


# ---- 上游常量 ----
MIMO_BASE = CFG.mimo_base

MODEL_MAP = CFG.model_map

# 上游真实存在的模型码（用于判断客户端传来的模型名是否合法）
KNOWN_MODELS = set(MODEL_MAP.values()) | set(
    CFG.get("mimo", "model_ids") or []) | {
    "mimo-v2.6-pro", "mimo-v2.6-flash",
    "mimo-v2.6-pro-ultraspeed-studio",
}

HEADERS_TPL = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN",
    "Content-Type": "application/json",
    "Origin": MIMO_BASE,
    "Referer": MIMO_BASE + "/",
    "x-timeZone": "Asia/Shanghai",
}

_DEFAULT_BASE = "https://aistudio.xiaomimimo.com"

DEFAULT_MODEL = CFG.get("mimo", "default_model", "mimo-v2.6-pro")
# 客户端填了上游不认识的模型名时怎么办
#   fallback    -> 用默认模型（推荐，AstrBot 等 WebUI 手填场景不会直接报错）
#   passthrough -> 原样发给上游（上游可能拒绝）
#   error       -> 直接返回 400 并列出可用模型
ON_UNKNOWN_MODEL = str(CFG.get("mimo", "on_unknown_model", "fallback")).lower()


def resolve_model(model):
    """把客户端传来的模型名解析成上游认识的模型码

    AstrBot / 各类 OpenAI 客户端常由用户手填模型名（gpt-4o、deepseek-chat…
    甚至随手起的别名）。原样透传给上游只会被上游拒绝，整条链路报错，
    而用户看到的只是一个笼统的"上游错误"，很难定位到"模型名不对"。

    解析顺序：精确命中 model_map -> 已是已知模型码 -> 按归一化的键再试
              -> 按 on_unknown_model 策略处理
    """
    m = (model or "").strip()
    if not m:
        return DEFAULT_MODEL, "empty"

    if m in MODEL_MAP:
        return MODEL_MAP[m], "map"

    if m in KNOWN_MODELS:
        return m, "known"

    # 归一化再试（忽略大小写 / _ - . 空格）
    def norm(x):
        return "".join(c for c in str(x).lower() if c not in "_-. ")
    nm = norm(m)
    for k, v in MODEL_MAP.items():
        if norm(k) == nm:
            return v, "map"
    for k in KNOWN_MODELS:
        if norm(k) == nm:
            return k, "known"

    # 都没命中
    if ON_UNKNOWN_MODEL == "error":
        raise ValueError(
            f"未知模型 '{m}'。可用模型: {', '.join(sorted(KNOWN_MODELS))}")
    if ON_UNKNOWN_MODEL == "passthrough":
        return m, "passthrough"
    log(f"⚠️ 未知模型 '{m}'，已回退到 {DEFAULT_MODEL}"
        f"（可在 config.json 设 mimo.default_model 改默认值）")
    return DEFAULT_MODEL, "fallback"


def _base():
    """上游 base

    优先级：
      1) config.json 里显式写了 mimo.base（且不是默认值）-> 用它
      2) 否则用模块常量 MIMO_BASE（可被运行时改写，测试依赖这一点）

    这样既保证"改配置生效"，又不破坏直接改 MIMO_BASE 的用法。
    """
    b = CFG.get("mimo", "base")
    if b and b.rstrip("/") != _DEFAULT_BASE:
        return b.rstrip("/")
    return MIMO_BASE


def _mimo_url(path, account):
    """所有 /open-apis 请求都必须带 ?xiaomichatbot_ph=，否则网关直接 401。
    实测: 不带 -> 401 loginUrl; 带上 -> 404 接口不存在（说明鉴权已过，进到路由层）
    """
    base = _base()
    if "xiaomichatbot_ph=" in path:
        return base + path
    sep = "&" if "?" in path else "?"
    return base + path + sep + "xiaomichatbot_ph=" + urllib.parse.quote(account.ph, safe="")


def _mimo_req(account, method, path, body=None, headers=None, stream=False, timeout=120):
    url = _mimo_url(path, account)
    hdrs = dict(HEADERS_TPL)
    hdrs["Cookie"] = account.cookie_header()
    if headers:
        hdrs.update(headers)

    dbg_request(method, url, hdrs, body, tag="上游")

    t0 = time.time()
    try:
        r = requests.request(
            method, url, json=body if body is not None else None,
            headers=hdrs, stream=stream, timeout=timeout,
        )
    except Exception as e:
        dbg_error(f"上游 {method} {path}", e)
        raise
    _force_utf8(r)
    dt = time.time() - t0

    # 流式响应不能提前读 body（会和调用方抢同一个流），
    # 非流式才把响应体记下来。
    try:
        if stream:
            dbg(f"◀── 上游 HTTP {r.status_code} ({dt:.2f}s, 流式, 响应体略)")
        else:
            dbg_response(r, tag=f"上游({dt:.2f}s)")
    except Exception:
        pass
    return r


# ============================================================
# 文件上传 / 文档解析（抓包 2026-10-29 实证）
# ============================================================
# 完整链路：
#   ① POST /open-apis/resource/genUploadInfo
#      body: {"fileName": "...", "fileContentMd5": "<32位hex>"}
#      -> {resourceId, resourceUrl, uploadUrl, objectName}
#   ② PUT uploadUrl
#      headers: Content-Type: application/octet-stream
#               content-md5: <同上的 md5>      ← 抓包里有，服务端会校验
#   ③ POST /open-apis/resource/parse
#      query: fileUrl=<urlencode(resourceUrl)>&objectName=<...>&model=<...>
#      -> {id, collectionName, bytes, filename, tokenUsage}
#   ④ POST /open-apis/react/chat
#      multiMedias: [{mediaType:"file", fileUrl:<resourceUrl>,
#                     name, size, status:"completed", objectName,
#                     url:<collectionName>,   ← ★ 关键，不是 resourceUrl
#                     tokenUsage}]
#
# 最容易错的地方：④ 的 url 字段是 ③ 返回的 collectionName，
# 填 resourceUrl 会让模型拿不到文档内容。

# bot/config 里 fileUpload 的限制（实测值）
FILE_UPLOAD_TYPES = ["text/plain", "application/pdf",
                     "image/jpeg", "image/png"]
FILE_MAX_MB = 100
FILE_MAX_COUNT = 50

# 扩展名 -> MIME（用于判断能否上传）
_EXT_MIME = {
    "txt": "text/plain", "md": "text/plain", "csv": "text/plain",
    "log": "text/plain", "json": "text/plain", "html": "text/plain",
    "pdf": "application/pdf",
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",
}


def file_mime(filename, data=None):
    """按扩展名判断 MIME；认不出时按内容猜"""
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    if data:
        if data[:4] == b"%PDF":
            return "application/pdf"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:2] == b"\xff\xd8":
            return "image/jpeg"
    return "application/octet-stream"


def upload_resource(account, data, filename):
    """上传文件到对象存储，返回 dict

    {"resourceUrl", "uploadUrl", "objectName", "resourceId", "md5", "mime"}

    与音频上传同一套接口，只是多了 fileContentMd5 / content-md5
    （抓包实证：无论什么文件类型都带）。
    """
    if not data:
        raise RuntimeError("上传内容为空")
    md5 = hashlib.md5(data).hexdigest()
    body = {"fileName": filename, "fileContentMd5": md5}
    r = _mimo_req(account, "POST", "/open-apis/resource/genUploadInfo",
                  body=body, timeout=30)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(
            f"genUploadInfo 响应不是 JSON（前 120 字符: {resp_text(r)[:120]!r}）")
    d = j.get("data") or {}
    resource_url = d.get("resourceUrl")
    upload_url = d.get("uploadUrl")
    object_name = d.get("objectName")
    if not (resource_url and upload_url):
        raise RuntimeError(f"genUploadInfo 返回异常: {json.dumps(j, ensure_ascii=False)[:200]}")

    hdrs = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)),
        "content-md5": md5,          # ★ 抓包实证
    }
    dbg_request("PUT", upload_url, hdrs, f"<{len(data)} 字节 {filename}>", tag="对象存储")
    pr = requests.put(upload_url, data=data, headers=hdrs, timeout=180)
    dbg(f"◀── 对象存储 HTTP {pr.status_code}")
    if pr.status_code >= 400:
        raise RuntimeError(
            f"文件上传失败 HTTP {pr.status_code}: {resp_text(pr)[:200]}")
    return {
        "resourceUrl": resource_url,
        "uploadUrl": upload_url,
        "objectName": object_name,
        "resourceId": d.get("resourceId"),
        "md5": md5,
        "mime": file_mime(filename, data),
        "size": len(data),
    }


def parse_resource(account, resource_url, object_name, model=None):
    """解析已上传的文件，返回 dict

    {"collectionName", "id", "bytes", "filename", "tokenUsage"}

    collectionName 要填进 chat 的 multiMedias[].url。
    """
    qs = urllib.parse.urlencode({
        "fileUrl": resource_url,
        "objectName": object_name,
        "model": model or CFG.get("mimo", "model", "mimo-v2.6-pro"),
    })
    r = _mimo_req(account, "POST", "/open-apis/resource/parse?" + qs,
                  body={}, timeout=120)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(
            f"resource/parse 响应不是 JSON（前 120 字符: {resp_text(r)[:120]!r}）")
    if j.get("code") != 0:
        raise RuntimeError(f"文档解析失败: {json.dumps(j, ensure_ascii=False)[:200]}")
    d = j.get("data") or {}
    coll = d.get("collectionName") or d.get("id")
    if not coll:
        raise RuntimeError(f"resource/parse 未返回 collectionName: {json.dumps(d, ensure_ascii=False)[:200]}")
    return {
        "collectionName": coll,
        "id": d.get("id"),
        "bytes": d.get("bytes"),
        "filename": d.get("filename"),
        "tokenUsage": d.get("tokenUsage"),
    }


def attach_file(account, data, filename, model=None):
    """一步到位：上传 + 解析，返回可直接塞进 multiMedias 的 dict"""
    up = upload_resource(account, data, filename)
    try:
        pr = parse_resource(account, up["resourceUrl"], up["objectName"], model)
    except Exception as e:
        # 解析失败也能带着原文件聊（模型可能仍能看图/读部分内容）
        log(f"文档解析失败，改为直接附带原文件: {str(e)[:120]}")
        pr = {"collectionName": "", "tokenUsage": None}
    return {
        "mediaType": "file",
        "fileUrl": up["resourceUrl"],
        "compressedVideoUrl": "",
        "audioTrackUrl": "",
        "name": filename,
        "size": up["size"],
        "status": "completed",
        "objectName": up["objectName"],
        "url": pr.get("collectionName") or "",
        "tokenUsage": pr.get("tokenUsage"),
    }


def get_user_info(account):
    """GET /open-apis/user/mi/get —— 账号状态

    抓包实证返回：userId / userName / idc / bannedStatus /
    mimoClawBannedStatus / tokenPlan / agreement / reactEnabled ...
    bannedStatus != NOT_BANNED 说明号被封了，值得在面板上标出来。
    """
    try:
        r = _mimo_req(account, "GET", "/open-apis/user/mi/get", timeout=20)
        j = r.json()
    except Exception as e:
        return {"error": str(e)[:200]}
    if j.get("code") != 0:
        return {"error": json.dumps(j, ensure_ascii=False)[:200]}
    d = j.get("data") or {}
    return {
        "userId": d.get("userId"),
        "userName": d.get("userName"),
        "idc": d.get("idc"),
        "banned": d.get("bannedStatus"),
        "clawBanned": d.get("mimoClawBannedStatus"),
        "tokenPlan": d.get("tokenPlan"),
        "reactEnabled": d.get("reactEnabled"),
        "isChildAccount": d.get("isChildAccount"),
    }


def claw_create(account):
    """POST /open-apis/user/mimo-claw/create —— 申请容器

    抓包实证状态机：CREATING -> AVAILABLE / CREATE_FAILED
    失败时 failCode=CONTAINER_CREATE_FAILED，message 会说明原因
    （如"使用中机器已达上限，请稍后重试"）。
    """
    try:
        r = _mimo_req(account, "POST", "/open-apis/user/mimo-claw/create",
                      timeout=30)
        j = r.json()
    except Exception as e:
        return {"error": str(e)[:200]}
    if j.get("code") != 0:
        return {"error": json.dumps(j, ensure_ascii=False)[:200]}
    d = j.get("data") or {}
    return {
        "status": d.get("status"),
        "message": d.get("message"),
        "failCode": d.get("failCode"),
        "requestId": d.get("requestId"),
        "cluster": d.get("clusterKey"),
    }


def claw_container(account, wait_available=False, timeout=90, poll=3.0):
    """mimo-claw 容器状态（带可选的创建后轮询）

    抓包实证：create 返回 status=CREATING，需要轮询 status 端点
    等它变成 AVAILABLE；失败时 failCode=CONTAINER_CREATE_FAILED。
    """
    def _st():
        try:
            r = _mimo_req(account, "GET",
                          "/open-apis/user/mimo-claw/status", timeout=20)
            j = r.json()
        except Exception as e:      # noqa: BLE001
            return {"error": str(e)[:200]}
        if j.get("code") != 0:
            return {"error": j.get("msg") or f"HTTP {r.status_code}"}
        d = j.get("data") or {}
        b = d.get("billing") or {}
        fq = b.get("freeQuota") or {}
        return {
            "status": d.get("status"),
            "message": d.get("message"),
            "cluster": d.get("clusterKey"),
            "expireTime": d.get("expireTime"),
            "edition": b.get("edition"),
            "freeQuota": {
                "used": fq.get("usedTokens"),
                "limit": fq.get("limitTokens"),
                "remaining": fq.get("remainingTokens"),
                "percent": fq.get("usagePercent"),
                "exhausted": fq.get("exhausted"),
            },
        }

    st = _st()
    if not wait_available or st.get("status") != "CREATING":
        return st
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        _t.sleep(poll)
        st = _st()
        if st.get("status") != "CREATING":
            return st
    st.setdefault("message", "等待容器就绪超时")
    return st


def claw_files(account, action="list", path="", extra=None,
               content=None, filename=""):
    """mimo-claw 沙箱文件操作（host-files）

    支持的 action：list / download / preview / upload / delete

    ⚠️ 端点路径来自早期抓包的**名称记录**，未完整实证。
    config.json 的 claw.host_files_prefix 可改前缀。

    upload 时可用 content(二进制) + filename 直接传内容。
    失败返回带 error 与 hint 的 dict，不抛异常。
    """
    import base64 as _b
    import mimo_proxy.config as _c
    prefix = _c.CFG.get("claw", "host_files_prefix") or \
        "/open-apis/user/mimo-claw/host-files"
    url = f"{prefix.rstrip('/')}/{action}"
    body = dict(extra or {})
    if path:
        body.setdefault("path", path)
    if action == "upload" and content is not None:
        body.setdefault("fileName", filename or (path or "file").split("/")[-1])
        body.setdefault("content", _b.b64encode(content).decode())
    try:
        r = _mimo_req(account, "POST", url, body=body or None, timeout=30)
        j = r.json()
    except Exception as e:      # noqa: BLE001
        return {"error": str(e)[:200], "url": url,
                "hint": "路径未经实证，请抓包确认 host-files 端点"}
    if j.get("code") != 0:
        return {"error": j.get("msg") or f"HTTP {r.status_code}", "url": url}
    d = j.get("data") or {}
    out = {"url": url, "data": d}
    # download/preview 常返回签名 URL，便于面板直接打开
    for k in ("downloadUrl", "url", "fileUrl", "previewUrl", "signedUrl"):
        if isinstance(d, dict) and d.get(k):
            out["link"] = d[k]
            break
    return out


def claw_upload_file(account, filename, content):
    """上传文件到 mimo-claw 沙箱（走通用上传通道，非 host-files）

    复用已实证的 genUploadInfo + PUT 链路（与 ASR/图片同一套），
    返回可放进对话 multiMedias 的结构。比 host-files/upload 更可靠。
    """
    return upload_resource(account, content, filename)


def claw_conversations(account, page=1, size=20):
    """mimo-claw 沙箱会话列表

    ★ 抓包实证两个要点：

      1) conversation/list 传 type="claw" 才返回沙箱会话
         （普通聊天是 "chat"，两种会话分开存）
      2) 分页参数必须用 pageInfo 嵌套：
         {"type":"claw","pageInfo":{"pageNum":1,"pageSize":20}}
         早期版本发 {"page":1,"size":20} 会 500。

      另外与 list_conversations 同理，会话可能只在 fastchat
      通道返回，故两边都查并去重合并。
    """
    def _q(path):
        try:
            r = _mimo_req(account, "POST", path,
                          body={"type": "claw",
                                "pageInfo": {"pageNum": page, "pageSize": size}},
                          timeout=20)
            j = r.json()
        except Exception as e:      # noqa: BLE001
            return None, str(e)[:120]
        if j.get("code") != 0:
            return None, j.get("msg") or f"HTTP {r.status_code}"
        d = j.get("data") or {}
        items = d.get("dataList") or d.get("list") or d.get("records") or []
        return items, d.get("total", len(items))

    seen, total, errs = {}, 0, []
    for path in ("/open-apis/chat/conversation/list",
                 "/fastchat/open-apis/chat/conversation/list"):
        items, t = _q(path)
        if items is None:
            errs.append(f"{path}: {t}")
            continue
        total = max(total, t or 0)
        for it in items:
            if not isinstance(it, dict):
                continue
            cid = it.get("conversationId")
            if not cid or cid in seen:
                continue
            seen[cid] = {
                "conversationId": cid,
                "title": it.get("title"),
                "type": it.get("type", "claw"),
                "createTime": it.get("createTime"),
                "updateTime": it.get("updateTime"),
            }
    return {
        "total": total or len(seen),
        "items": list(seen.values()),
        "errors": errs or None,
    }


def claw_dialog(account, conversation_id, end_id="", page=1, size=50):
    """读取 mimo-claw 沙箱会话的历史消息

    ★ 抓包实证：body 是**双层包装**，早期版本发平的
      {"page":1,...} 会 500：

        POST /fastchat/open-apis/chat/dialog/list
        body: {"queryParam": {"conversationId":"...","endId":"..."},
               "pageInfo":  {"pageNum":1,"pageSize":10}}
        resp: data.dataList / data.total
    """
    if not conversation_id:
        return {"error": "缺少 conversationId"}
    body = {
        "queryParam": {"conversationId": conversation_id, "endId": end_id},
        "pageInfo": {"pageNum": page, "pageSize": size},
    }
    try:
        r = _mimo_req(account, "POST",
                      "/fastchat/open-apis/chat/dialog/list",
                      body=body, timeout=25)
        j = r.json()
    except Exception as e:      # noqa: BLE001
        return {"error": str(e)[:200]}
    if j.get("code") != 0:
        return {"error": j.get("msg") or f"HTTP {r.status_code}"}
    d = j.get("data") or {}
    raw = d.get("dataList") or d.get("list") or []
    msgs = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        msgs.append({
            "msgId": it.get("msgId") or it.get("id"),
            "role": it.get("role") or it.get("senderType"),
            "content": it.get("content") or it.get("query") or "",
            "reasoningContent": it.get("reasoningContent") or "",
            "createTime": it.get("createTime"),
        })
    return {"total": d.get("total", len(msgs)), "messages": msgs}


def claw_release(account):
    """释放/销毁 mimo-claw 容器

    ⚠️ 此端点未经实证（早期抓包只记录了 create/status）。
    失败时返回带 error 的 dict 并提示，不会抛异常。
    config.json 的 claw.release_path 可改写路径。
    """
    import mimo_proxy.config as _c
    path = _c.CFG.get("claw", "release_path") or \
        "/open-apis/user/mimo-claw/release"
    try:
        r = _mimo_req(account, "POST", path, timeout=30)
        j = r.json()
    except Exception as e:      # noqa: BLE001
        return {"error": str(e)[:200], "path": path,
                "hint": "释放端点未经实证，请抓包确认"}
    if j.get("code") != 0:
        return {"error": j.get("msg") or f"HTTP {r.status_code}", "path": path}
    d = j.get("data") or {}
    return {"path": path, "status": d.get("status"), "message": d.get("message")}


def ensure_container(account, auto_create=True, wait=True, timeout=90):
    """确保账号有一个可用容器（无则自动申请）

    容器状态机（抓包实证）：
        无容器(null/NOT_FOUND) --create--> CREATING --轮询--> AVAILABLE
        失败分支：CREATING --超时/配额不足--> CREATE_FAILED

    注意：本文件为普通字符串，ASCII 图里不要出现反斜杠，
    否则 Python 3.12+ 会报 SyntaxWarning（非法转义序列）。

    auto_create=False 时只查询不申请。
    结果按 uid 缓存 TTL 秒，避免每次对话都打上游。
    """
    import time as _t
    import mimo_proxy.config as _c

    ttl = int(_c.CFG.get("claw", "ensure_cache_ttl", 120) or 0)
    key = str(getattr(account, "uid", "") or "")
    now = _t.time()
    if ttl > 0:
        c = _CLAW_CACHE.get(key)
        if c and now - c[0] < ttl and c[1].get("status") == "AVAILABLE":
            return dict(c[1], cached=True)

    st = claw_container(account)
    status = st.get("status")
    if status == "AVAILABLE":
        if ttl > 0:
            _CLAW_CACHE[key] = (now, st)
        return st
    # ★ CREATING 也要继续等：卡在创建中时若直接返回，
    #   调用方拿到的就是不可用的容器，等于白查一次。
    if status == "CREATING":
        if not wait:
            return st
        st = claw_container(account, wait_available=True,
                            timeout=timeout, poll=3.0)
        if ttl > 0 and st.get("status") == "AVAILABLE":
            _CLAW_CACHE[key] = (now, st)
        return st
    if not auto_create:
        return st
    if "error" in st and status:
        return st
    # 无容器 -> 申请
    created = claw_create(account)
    if created.get("error"):
        return {"status": status, "create": created,
                "error": created.get("error")}
    if not wait:
        return {"status": "CREATING", "create": created}
    st = claw_container(account,
                        wait_available=bool(
                            _c.CFG.get("claw", "create_wait_timeout", 90)),
                        timeout=int(
                            _c.CFG.get("claw", "create_wait_timeout", 90)),
                        poll=3.0)
    st["create"] = created
    if ttl > 0:
        _CLAW_CACHE[key] = (now, st)
    return st


_CLAW_CACHE = {}


def get_account_quota(account):
    """查询账号配额（mimo-claw 状态 + 存储用量）。

    抓包实证的两个端点：
      GET /open-apis/user/mimo-claw/status
        -> data.status=AVAILABLE, data.billing.freeQuota.limitTokens=20000000
           data.clusterKey=OVERSEA, data.expireTime=毫秒时间戳
      GET /open-apis/user/mimo-claw/quotaUsage
        -> data.usedBytes / maxBytes / usagePercent

    失败不抛异常，返回 {"error": ...}，调用方按需展示。
    """
    out = {"uid": getattr(account, "uid", None)}
    # ---- 1. claw 状态 / token 配额 ----
    try:
        r = _mimo_req(account, "GET", "/open-apis/user/mimo-claw/status", timeout=20)
        j = r.json()
        d = j.get("data") or {}
        if j.get("code") == 0:
            b = d.get("billing") or {}
            fq = b.get("freeQuota") or {}
            out["status"] = d.get("status")
            out["message"] = d.get("message")
            out["cluster"] = d.get("clusterKey")
            out["expireTime"] = d.get("expireTime")
            out["edition"] = b.get("edition")
            out["apiKeyRequired"] = (b.get("apiKey") or {}).get("required")
            out["freeQuota"] = {
                "enabled": fq.get("enabled"),
                "exhausted": fq.get("exhausted"),
                "used": fq.get("usedTokens"),
                "limit": fq.get("limitTokens"),
                "remaining": fq.get("remainingTokens"),
                "percent": fq.get("usagePercent"),
                "period": fq.get("period"),
            }
            tp = b.get("tokenPlan") or {}
            out["tokenPlan"] = tp.get("status")
        else:
            out["error"] = j.get("msg") or f"HTTP {r.status_code}"
    except Exception as e:      # noqa: BLE001
        out["error"] = f"status: {e}"

    # ---- 2. 存储用量 ----
    try:
        r = _mimo_req(account, "GET", "/open-apis/user/mimo-claw/quotaUsage", timeout=20)
        j = r.json()
        d = j.get("data") or {}
        if j.get("code") == 0:
            out["storage"] = {
                "used": d.get("usedBytes"),
                "max": d.get("maxBytes"),
                "remaining": d.get("remainingBytes"),
                "percent": d.get("usagePercent"),
            }
    except Exception as e:      # noqa: BLE001
        out.setdefault("storageError", str(e))

    return out


def ws_ticket(account):
    """获取 WebSocket 连接票据

    ★ 抓包实证：GET /open-apis/user/ws/ticket -> data.ticket，
      之后连 wss://<host>/ws/proxy?ticket=<ticket> 建立长连接。
      claw 沙箱的实时输出（命令回显等）走这条通道。

    票据通常有时效，不要长期缓存。
    """
    try:
        r = _mimo_req(account, "GET", "/open-apis/user/ws/ticket", timeout=15)
        j = r.json()
    except Exception as e:      # noqa: BLE001
        return {"error": str(e)[:200]}
    if j.get("code") != 0:
        return {"error": j.get("msg") or f"HTTP {r.status_code}"}
    d = j.get("data") or {}
    return {"ticket": d.get("ticket"), "expire": d.get("expireTime"),
            "host": d.get("host") or d.get("wsHost")}


def claw_quota_all(accounts=None):
    """汇总所有账号的 mimo-claw 配额与存储

    用于面板一眼看出剩余额度总量，避免逐个点开看。
    单个账号失败不影响其它账号。
    """
    import mimo_proxy.config as _c
    if accounts is None:
        import mimo_proxy.server as _srv
        accounts = list(_srv.POOL.accounts)
    items, tot = [], {"used": 0, "limit": 0, "remaining": 0,
                      "storageUsed": 0, "storageMax": 0}
    for a in accounts or []:
        q = get_account_quota(a)
        q["uid"] = getattr(a, "uid", None)
        fq = q.get("freeQuota") or {}
        for sk, qk in (("used", "used"), ("limit", "limit"),
                       ("remaining", "remaining")):
            v = fq.get(qk)
            if isinstance(v, (int, float)):
                tot[qk] += v
        st = q.get("storage") or {}
        for sk, qk in (("used", "storageUsed"), ("max", "storageMax")):
            v = st.get(sk)
            if isinstance(v, (int, float)):
                tot[qk] += v
        items.append(q)
    n = len(items) or 1
    tot["accounts"] = len(items)
    tot["available"] = sum(1 for i in items
                           if i.get("status") == "AVAILABLE")
    if tot["limit"]:
        tot["percent"] = round(tot["used"] * 100.0 / tot["limit"], 2)
    else:
        tot["percent"] = 0
    tot["avgRemaining"] = int(tot["remaining"] / n)
    return {"accounts": items, "total": tot}


def fetch_bot_config(account):
    """拉取官方 bot 配置（模型清单 + 默认参数 + 音色）。

    ★ HAR 实证：GET /open-apis/bot/config，无需认证参数，
      响应 data.modelConfigList 是权威模型清单（15 个），
      每项含 temperature / topP / thinkingDefaultOn /
      webSearchDefaultStatus / fileUpload 限制 / nParam / nToken。
      还有 ttsVoices（音色清单）与 ext（功能开关）。

    失败返回 None，调用方须能容忍。
    """
    try:
        r = _mimo_req(account, "GET", "/open-apis/bot/config", timeout=20)
        j = r.json()
    except Exception:      # noqa: BLE001
        return None
    if j.get("code") != 0:
        return None
    d = j.get("data") or {}
    models = []
    for m in d.get("modelConfigList") or []:
        models.append({
            "model": m.get("model"),
            "name": m.get("name"),
            "temperature": m.get("temperature"),
            "topP": m.get("topP"),
            "thinking": m.get("thinkingDefaultOn"),
            "web": m.get("webSearchDefaultStatus"),
            "intro": m.get("cnIntro") or m.get("enIntro"),
            "params": m.get("nParam"),
            "context": m.get("nToken"),
            "vision": "VISION_QA" in (m.get("enabledSceneTypeList") or []),
        })
    voices = [{"id": v.get("id"), "name": v.get("name"),
               "desc": v.get("description")}
              for v in (d.get("ttsVoices") or [])]
    return {"models": models, "voices": voices,
            "ext": d.get("ext") or {},
            "raw_total": len(d.get("modelConfigList") or [])}


def list_conversations(account, conv_type="chat", page=1, size=20,
                       include_fastchat=True):
    """列出会话。

    ★ HAR 实证（2026-10-24）——两套通道，数据不在同一边：

      POST /open-apis/chat/conversation/list
      body: {"pageInfo":{"pageNum":1,"pageSize":20}}
      resp: {"code":0,"data":{"total":0,"pageNum":1,"dataList":[]}}   ← 空

      POST /fastchat/open-apis/chat/conversation/list
      body: {"pageInfo":{"pageNum":1,"pageSize":20}}
      resp: {"code":0,"data":{"total":2,"dataList":[...]}}            ← 有数据

    同一账号的会话只在 **fastchat** 通道返回，普通通道 total=0。
    所以默认两边都查并合并，按 conversationId 去重。

    pageInfo 必须存在（pageNum/pageSize 嵌套在里面）——
    早期版本直接发 {"page":1,"pageSize":20} 会 500。
    """
    def _q(path):
        try:
            r = _mimo_req(account, "POST", path,
                          body={"pageInfo": {"pageNum": page, "pageSize": size}},
                          timeout=20)
            j = r.json()
            if j.get("code") != 0:
                return None, (j.get("msg") or f"HTTP {r.status_code}")
            d = j.get("data") or {}
            return (d.get("dataList") or []), None
        except Exception as e:      # noqa: BLE001
            return None, str(e)[:120]

    merged, seen, errs = [], set(), []
    for path in ("/open-apis/chat/conversation/list",
                 "/fastchat/open-apis/chat/conversation/list"
                 if include_fastchat else None):
        if not path:
            continue
        lst, err = _q(path)
        if err:
            errs.append(f"{path}: {err}")
            continue
        for it in lst or []:
            cid = it.get("conversationId")
            if cid and cid not in seen:
                seen.add(cid)
                merged.append(it)

    return {"total": len(merged), "list": merged,
            "errors": errs, "fastchat_checked": include_fastchat}


def list_dialogs(account, conversation_id, end_id=None, page=1, size=10):
    """列出某个会话的历史消息。

    ★ 正确 body 格式（HAR 实证）—— queryParam + pageInfo 双层包装：

      POST /open-apis/react/dialog/list
      body: {"queryParam":{"conversationId":"378a...","endId":"32216683"},
             "pageInfo":{"pageNum":1,"pageSize":10}}

    早期版本发 {"page":1,"pageSize":20} → 500 服务器内部错误。

    同样优先 fastchat 通道（普通通道可能返回空数组）。
    """
    body = {
        "queryParam": {"conversationId": conversation_id},
        "pageInfo": {"pageNum": page, "pageSize": size},
    }
    if end_id:
        body["queryParam"]["endId"] = end_id
    for path in ("/open-apis/react/dialog/list",
                 "/fastchat/open-apis/chat/dialog/list"):
        try:
            r = _mimo_req(account, "POST", path, body=body, timeout=20)
            j = r.json()
            if j.get("code") != 0:
                continue
            data = j.get("data")
            if data:
                return data
        except Exception:      # noqa: BLE001
            continue
    return []


def upload_media(account, b64data, mime_type, media_type="image", filename=None):
    """上传图片/文件，返回可直接放进 multiMedias 的 dict

    ★ 协议已按 2026-10-29 抓包重写。旧实现有两个致命错误：
        1) 端点写错：/open-apis/react/genUploadInfo、
           /open-apis/react/resource/parse —— 上游根本没有这两个
        2) 返回结构错：只给 {resourceId, mediaType}，而上游要的是
           {mediaType, fileUrl, name, size, status, objectName,
            url:<collectionName>, tokenUsage}
           少了 fileUrl / url，模型根本拿不到文件内容
    """
    raw = base64.b64decode(b64data) if isinstance(b64data, str) else b64data
    if not raw:
        raise RuntimeError("上传内容为空")
    name = filename or ("upload." + (mime_type or "bin").rsplit("/", 1)[-1])
    obj = attach_file(account, raw, name)
    obj["mediaType"] = media_type or "file"
    return obj


def extract_medias(messages):
    """从 OpenAI messages 里抽出图片(base64/http)和文本文件, 返回 (query_text, multimedias_spec)"""
    # 这里只做"抽取", 实际上传需要 account, 放到 handle 里做
    images_b64 = []
    images_url = []
    text_files = []
    query_text = ""

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            if role == "user":
                query_text = content  # 简化: 取最后一条 user 文本
            continue
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    if role == "user":
                        query_text = part.get("text", "")
                elif part.get("type") == "image_url":
                    img = part.get("image_url", {})
                    url = img.get("url", "") if isinstance(img, dict) else str(img)
                    if url.startswith("http://") or url.startswith("https://"):
                        images_url.append(url)
                    elif url.startswith("data:"):
                        # data:image/png;base64,xxxx
                        m = re.match(r"data:([^;]+);base64,(.+)", url, re.S)
                        if m:
                            images_b64.append({"b64": m.group(2), "mime": m.group(1)})
                elif part.get("type") == "file":
                    # 文本文件
                    f = part.get("file", {})
                    if isinstance(f, dict):
                        data_uri = f.get("file_data", "") or f.get("data", "")
                        m = re.match(r"data:([^;]+);base64,(.+)", data_uri, re.S)
                        if m:
                            text_files.append({
                                "b64": m.group(2),
                                "mime": m.group(1),
                                "filename": f.get("filename", "file.txt"),
                            })
    return query_text, images_b64, images_url, text_files


def _content_to_text(content):
    """把 message.content（str 或 list）统一成文本"""
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "\n".join(parts)
    return content or ""


def _normalize_roles(messages):
    """把 OpenAI 变体 role 归一成标准 role

    deepseek-harness / 部分客户端会用 `developer` role（OpenAI o1 之后
    引入，语义等同 system）。上游只认 system/user/assistant，
    不归一的话这条系统指令会被当成普通消息甚至丢掉。
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        r = m.get("role")
        if r == "developer":
            m = dict(m)
            m["role"] = "system"
        out.append(m)
    return out


def build_mimo_payload(messages, model, tools=None, reasoning_effort="default",
                       conversation_id=None, history_mode="full",
                       temperature=None, top_p=None, web_search=None,
                       tool_choice=None, parallel_tool_calls=True):
    """构造上游 payload。

    conversation_id: 传入则复用会话（上游可记住上文）；
                     不传则自动生成新的。
    history_mode:
        "full"  —— 把历史对话拼进 query（默认，最稳）。
                   上游 query 是单轮字段，不拼的话历史会丢。
        "last"  —— 只发最后一条（省 token，依赖上游会话记忆）。
        "auto"  —— 有 conversation_id 时只发最后一条，否则拼全量。
    """
    # ★ claw 沙箱能力追加进工具清单（claw.expose_tools 开启时）。
    #   必须在 serialize / inject 之前做，否则提示词里没有这些工具，
    #   tool_names 白名单也不认，模型调了会被当成误判丢弃。
    try:
        from .tools import append_claw_tools
        tools = append_claw_tools(tools)
    except Exception:      # noqa: BLE001
        pass
    messages = _normalize_roles(messages)
    system = ""
    # ★ 用 serialize_tool_turns 而不是简单过滤 role:
    #   它把 assistant 的 tool_calls 和 role="tool" 的结果都保留下来，
    #   否则模型看不到自己调过什么、结果是什么 —— 多轮工具调用直接断链。
    turns = []          # [(role, text)]
    for role, text in serialize_tool_turns(messages):
        if role == "system":
            # 多条 system 拼接，而不是互相覆盖
            system = (system + "\n" + text).strip() if system else text
        elif role in ("user", "assistant") and text:
            turns.append((role, text))

    if tools:
        # MCP 场景工具动辄几十个，不限制会把提示词撑到 20K+ 字符
        system = inject_tools_prompt(
            system, tools,
            max_tools=int(CFG.get("mimo", "max_tools", 32) or 0) or None,
            max_desc=int(CFG.get("mimo", "max_tool_desc", 160) or 0) or None,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls)

    mapped, _how = resolve_model(model)
    enable_thinking = reasoning_effort not in ("none", "minimal")

    if history_mode == "auto":
        history_mode = "last" if conversation_id else "full"

    if turns:
        last_role, last_text = turns[-1]
        history = turns[:-1]
    else:
        last_role, last_text = ("user", "")
        history = []

    if history_mode == "last" or not history:
        query = last_text
    else:
        # 把历史拼成可读的对话块，末尾是当前问题
        lines = []
        for role, text in history:
            if not text:
                continue
            tag = "用户" if role == "user" else "助手"
            lines.append(f"{tag}: {text}")
        block = "\n".join(lines)
        query = (f"[对话历史]\n{block}\n[/对话历史]\n\n"
                 f"{'用户' if last_role == 'user' else '助手'}: {last_text}")

    if system:
        query = f"[System: {system}]\n\n{query}"

    # ★ temperature / topP 之前根本没传给上游 —— 客户端设了不生效。
    #   抓包实证上游 modelConfig 形如：
    #   {"enableThinking":false,"model":"mimo-v2.6-pro-ultraspeed-studio",
    #    "temperature":0.8,"topP":0.95,"webSearchStatus":"disabled"}
    mc = {
        "enableThinking": enable_thinking,
        "webSearchStatus": ("enabled" if web_search else "disabled"),
        "model": mapped,
    }
    try:
        if temperature is not None:
            t = float(temperature)
            if 0.0 <= t <= 2.0:
                mc["temperature"] = t
    except (TypeError, ValueError):
        pass
    try:
        if top_p is not None:
            pp = float(top_p)
            if 0.0 < pp <= 1.0:
                mc["topP"] = pp
    except (TypeError, ValueError):
        pass

    payload = {
        "msgId": gen_msg_id(),
        "conversationId": conversation_id or gen_conv_id(),
        "query": query,
        "isEditedQuery": False,
        "modelConfig": mc,
        "multiMedias": [],
    }
    return payload, mapped


def call_mimo_stream(account, payload, idle_timeout=None,
                     heartbeat=None):
    """增量读取上游 SSE，边收到边产出事件。

    这是"真流式"的关键：旧实现 call_mimo() 会把上游整个响应读完再返回，
    导致客户端在同一瞬间收到全部 chunk —— 深度思考时间显示为 0，
    也没有逐字输出的效果。这里改为逐个事件产出。

    心跳与空闲超时 ★
        上游在深度思考时可能几十秒不发任何数据。若直接
        `for line in r.iter_lines()`，会一直阻塞在读取上，
        既发不出心跳也检查不了超时 —— 客户端/中间设备往往先超时断开。
        所以这里用后台线程读、主线程带 1s 超时取队列：
        静默期间每秒醒来一次，发心跳注释行并检查空闲超时。

    idle_timeout: 上游多久没产出新事件就放弃（秒）。
    heartbeat:    静默期间每隔多少秒 yield 一个 ("__heartbeat__", None)。
                  0/None 关闭。

    yield: (event_type:str|None, data:dict)
           event_type == "__heartbeat__" 时 data 为 None，调用方忽略即可
    """
    if idle_timeout is None:
        idle_timeout = int(CFG.get("mimo", "stream_idle_timeout", 300))
    if heartbeat is None:
        heartbeat = int(CFG.get("mimo", "heartbeat_interval", 15))
    idle_timeout = idle_timeout if idle_timeout and idle_timeout > 0 else None
    heartbeat = heartbeat if heartbeat and heartbeat > 0 else None

    path = f"/open-apis/react/chat?xiaomichatbot_ph={urllib.parse.quote(account.ph, safe='')}"
    r = _mimo_req(account, "POST", path, body=payload, stream=True)

    # ★ HTTP 状态码必须在启动后台线程**之前**检查。
    #   之前这里 try/except pass 把 401/403/500 全吞了：上游返回
    #   风控页或裸 JSON 错误时，响应体里没有 "data:" 前缀，
    #   解析出 0 个事件 -> 静默 [DONE] -> 客户端一片空白，
    #   而日志还写 "stream ok ... chunks=0"，真实原因彻底丢失。
    #
    #   放在 t.start() 之后还会和 reader 线程抢同一个流，
    #   导致 r.content 读到一半、错误时有时无。
    _sc = getattr(r, "status_code", 200)
    if not (200 <= _sc < 300):
        snippet = ""
        try:
            raw = b""
            try:
                raw = r.content[:400]
            except Exception:
                for ck in r.iter_content(400):
                    raw += ck
                    if len(raw) >= 400:
                        break
            snippet = raw.decode("utf-8", "replace")
        except Exception:
            pass
        try:
            r.close()
        except Exception:
            pass
        raise RuntimeError(
            f"上游 HTTP {_sc}；响应体前 400 字符: {snippet!r}")

    q = queue.Queue()

    def _reader():
        try:
            for line in r.iter_lines():
                q.put(("line", line))
        except Exception as e:      # noqa: BLE001 - 读流异常要传给主线程
            q.put(("err", e))
        finally:
            q.put(("end", None))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    cur_event = None
    last_at = time.time()
    next_hb = last_at + heartbeat if heartbeat else None
    raw_head = []        # 上游原始响应前若干字符，仅用于诊断
    produced = 0         # 已产出的事件数

    while True:
        try:
            # ★ 不能叫 payload —— 会和函数参数（请求体）同名被覆盖
            kind, item = q.get(timeout=1.0)
        except queue.Empty:
            # 静默：这是发心跳和判超时的唯一时机
            now = time.time()
            if next_hb and now >= next_hb:
                yield ("__heartbeat__", None)
                next_hb = now + heartbeat
            if idle_timeout and now - last_at > idle_timeout:
                raise RuntimeError(
                    f"上游 {idle_timeout}s 无数据，已放弃"
                    f"（可能是深度思考超时，可调 mimo.stream_idle_timeout）")
            continue

        if kind == "err":
            raise item
        if kind == "end":
            break

        line = item
        now = time.time()
        if line:
            last_at = now
            if next_hb:
                next_hb = now + heartbeat
            sline = line.decode("utf-8", "replace").rstrip("\r")
            if len(raw_head) < 400:
                raw_head.append(sline[:400])
            if sline.startswith("event:"):
                cur_event = sline[6:].strip()
            elif sline.startswith("data:"):
                try:
                    data = json.loads(sline[5:].strip())
                except json.JSONDecodeError:
                    continue
                produced += 1
                yield (cur_event, data)
            elif sline.startswith("{"):
                # ★ 上游有时直接返回裸 JSON（错误/风控），不是 SSE。
                #   不解析的话调用方收到 0 个事件，客户端一片空白。
                try:
                    data = json.loads(sline)
                except json.JSONDecodeError:
                    continue
                produced += 1
                yield (cur_event or "error", data)
            continue

        # 空行：一个事件结束
        cur_event = None

    # 收尾：关闭响应，让后台线程退出
    try:
        r.close()
    except Exception:
        pass

    # ★ 一个事件都没产出 = 上游没给任何可用数据。
    #   静默返回会让客户端空白且日志还写 "ok"，必须明确报错。
    if produced == 0:
        head = " | ".join(raw_head)[:400]
        raise RuntimeError(
            f"上游流式响应未包含任何事件（可能不是 SSE 或已被风控拦截）；"
            f"响应前 400 字符: {head!r}")


def normalize_event(data):
    """把上游事件归一化为 [(kind, text), ...]

    ★ 返回列表而不是单个元组：一个事件可能同时含 reasoningContent 和
      content，只取一个会丢内容。

    ★ 判据用"非空"而不是"键存在"：上游在思考结束后仍会继续发
      {"reasoningContent": "", "content": "你"}，此时若用
      `"reasoningContent" in data` 判断，就会一直走 reasoning 分支
      返回空串，调用方 `if not val: continue` 再把整段正文丢掉 ——
      表现就是"客户端只看到思考，看不到正文"。

    kind: 'reasoning' | 'content' | 'usage' | 'error'
    """
    if not isinstance(data, dict):
        return []
    # usage 是 dict 而非文本，单独处理
    if "promptTokens" in data or "completionTokens" in data:
        return [("usage", data)]

    out = []
    if data.get("messageType") == "assistant":
        r = data.get("reasoningContent")
        c = data.get("content")
        # reasoning 在前，content 在后，符合阅读顺序
        if r:
            out.append(("reasoning", r))
        if c:
            out.append(("content", c))
        if out:
            return out
        return []

    # 上游偶发的 error 事件
    if data.get("type") == "error":
        return [("error", data)]
    code = data.get("code")
    if code not in (0, None) and ("msg" in data or "message" in data):
        return [("error", data)]
    return []


def parse_sse_events(raw):
    """把 SSE 文本解析成 [(event_type, data_dict)]"""
    out = []
    cur_event = None
    for line in raw.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event:"):
            cur_event = line[6:].strip()
        elif line.startswith("data:"):
            data_str = line[5:].strip()
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            out.append((cur_event, data))
    return out


def call_mimo(account, payload):
    path = f"/open-apis/react/chat?xiaomichatbot_ph={urllib.parse.quote(account.ph, safe='')}"
    r = _mimo_req(account, "POST", path, body=payload, stream=True)
    r.raise_for_status()
    # 不依赖 requests 的自动解码：直接拿 bytes 自行按 UTF-8 解码。
    # requests 对 text/event-stream（响应头无 charset）会回退 ISO-8859-1，
    # 导致中文变成 'ä½ å¥½'。这里彻底绕开该行为。
    buf = []
    for chunk in r.iter_lines():
        if chunk:
            buf.append(chunk.decode("utf-8", "replace"))
    return "\n".join(buf)