# -*- coding: utf-8 -*-
"""
TTS / ASR（异步任务队列模式，协议来自抓包实证）

TTS: chat 建会话 -> conversation/save ★入库 -> tts/v2/generate
     -> tts/generateStatus 轮询 -> 下载 wav
ASR: resource/genUploadInfo -> PUT 上传 -> asr/recognize
     -> asr/recognizeStatus 轮询 -> 取 data.text

★ 关键: 会话必须 save 才入库，否则一律 "conversation not exist"
"""

import os
import sys
import json
import time
import threading
import urllib.parse

import requests

from .config import CFG
from .utils import log, resp_text
from .mimo import call_mimo, _mimo_req, _mimo_url, HEADERS_TPL, gen_msg_id, gen_conv_id

# ---------------------------------------------------------------- TTS 模型
# 协议来自 2026-10-25 抓包实证。三种模式的差别在 content.messages[0]
# （user 位）的含义和 audio 字段：
#
#   标准 tts      : user = 语气/风格描述, audio = {format, voice: "音色名"}
#   音色克隆 vc   : user = ""            , audio = {format, voice: "<参考音频URL>"}
#   音色设计 vd   : user = 音色描述文字  , audio = {format}        ← 没有 voice
#
# ★ 注意标准/克隆/设计三者的 modelConfig 只有 modelCode，
#   scene 只在标准模式出现（BRIEF_DESCRIPTION）。
MODEL_TTS = "mimo-v2.5-tts"
MODEL_TTS_VC = "mimo-v2.5-tts-voiceclone"
MODEL_TTS_VD = "mimo-v2.5-tts-voicedesign"

# 官方别名（bot/config 的 redirectTo 里实证）
TTS_MODEL_ALIAS = {
    "mimo-v2.5-tts": MODEL_TTS,
    "mimo-v2.1-tts": MODEL_TTS,
    "mimo-v2.5-tts-vc": MODEL_TTS_VC,
    "mimo-v2.1-tts-vc": MODEL_TTS_VC,
    "mimo-v2.5-tts-voiceclone": MODEL_TTS_VC,
    "mimo-v2.1-tts-voiceclone": MODEL_TTS_VC,
    "mimo-v2.5-tts-vd": MODEL_TTS_VD,
    "mimo-v2.1-tts-vd": MODEL_TTS_VD,
    "mimo-v2.5-tts-voicedesign": MODEL_TTS_VD,
    "mimo-v2.1-tts-voicedesign": MODEL_TTS_VD,
}

# 官方音色清单（bot/config 的 data.ttsVoices，抓包实证）
# preview 是官方 CDN 上的试听片段，可直接在浏览器打开
# 官方音色清单 —— 抓包实证（bot/config 的 data.ttsVoices，2026-10-29）
# avatar / previewAudio 都是 CDN 上的真实资源，可直接打开/播放。
# 之前是按 slug 拼的 URL，这次换成服务端给的原值。
OFFICIAL_VOICES = [
    {"id": "冰糖", "desc": "活泼少女",
     "avatar": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/avatar-ng/bingtang.webp",
     "preview": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/audio/bingtang.wav"},
    {"id": "Mia", "desc": "Lively girl",
     "avatar": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/avatar-ng/mia.webp",
     "preview": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/audio/mia.wav"},
    {"id": "茉莉", "desc": "知性女声",
     "avatar": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/avatar-ng/moli.webp",
     "preview": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/audio/moli.wav"},
    {"id": "Chloe", "desc": "Sweet Dreamy",
     "avatar": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/avatar-ng/chloe.webp",
     "preview": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/audio/chloe.wav"},
    {"id": "苏打", "desc": "阳光少年",
     "avatar": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/avatar-ng/suda.webp",
     "preview": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/audio/suda.wav"},
    {"id": "Milo", "desc": "Sunny boy",
     "avatar": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/avatar-ng/milo.webp",
     "preview": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/audio/milo.wav"},
    {"id": "白桦", "desc": "成熟男声",
     "avatar": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/avatar-ng/baihua.webp",
     "preview": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/audio/baihua.wav"},
    {"id": "Dean", "desc": "Steady Gentle",
     "avatar": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/avatar-ng/dean.webp",
     "preview": "https://aistudio-cdn.xiaomimimo.com/xiaomimimo-static/tts/audio/dean.wav"},
]
OFFICIAL_VOICE_IDS = {v["id"] for v in OFFICIAL_VOICES}


# ------------------------------------------------------- voice 字段解析
# 一般 API 设计：三种模式只靠 model + voice 两个标准字段区分，
# 不需要任何私有字段。voice 的含义随 model 变：
#
#   mimo-v2.5-tts             -> 预设音色名（冰糖 / Mia / ...）
#   mimo-v2.5-tts-voicedesign -> 音色描述文字
#   mimo-v2.5-tts-voiceclone  -> 参考音频（base64 / data URI / http URL）
#
# 客户端只要能自定义 model 和 voice 就能用全部三种模式。
V_NAME = "name"      # 预设音色名
V_DESC = "desc"      # 描述文字
V_REF = "ref"        # 参考音频引用
V_BYTES = "bytes"    # 已解码的音频二进制


def classify_voice(value, mode, official_ids=None):
    """判断 voice 字段是什么，返回 (kind, payload)

    payload 依 kind 而定：V_NAME/V_DESC 是字符串，V_REF 是可下载的引用，
    V_BYTES 是已解出的二进制。
    """
    ids = official_ids if official_ids is not None else OFFICIAL_VOICE_IDS
    if value is None:
        return (None, None)
    # 已经是二进制（multipart 上传的参考音频）
    if isinstance(value, (bytes, bytearray)):
        return (V_BYTES, bytes(value))
    v = str(value).strip()
    if not v:
        return (None, None)

    # 音频引用：URL / data URI / 长 base64
    if v.startswith("http://") or v.startswith("https://"):
        return (V_REF, v)
    if v.startswith("data:"):
        return (V_REF, v)
    # 裸 base64：字符集像 base64 且能解出音频头。
    # 不靠长度下限判断——短的 wav 头也能解出特征，
    # 靠"解出来确实是音频"来判定更稳。
    import base64 as _b
    import re as _re
    if len(v) > 40 and not any(c in v for c in (" ", "\n", "\t")) and \
            _re.fullmatch(r"[A-Za-z0-9+/=]+", v):
        try:
            raw = _b.b64decode(v + "=" * (-len(v) % 4), validate=False)
            if len(raw) >= 12 and (
                raw[:4] in (b"RIFF", b"ID3", b"fLaC", b"OggS")
                or raw[:4] == b"\x1a\x45\xdf\xa3"
                or (raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0)
                or (raw[4:8] == b"ftyp")
            ):
                return (V_REF, v)
        except Exception:
            pass

    # 预设音色名：大小写不敏感
    for name in ids:
        if v.lower() == str(name).lower():
            return (V_NAME, name)

    # 剩下的一律当描述文字（vd 模式）；标准模式则未知
    return (V_DESC, v)


def resolve_tts_model(model):
    """把客户端传的模型名（含别名）归一到三种标准代号"""
    if not model:
        return MODEL_TTS
    m = str(model).strip()
    if m in TTS_MODEL_ALIAS:
        return TTS_MODEL_ALIAS[m]
    low = m.lower()
    for k, v in TTS_MODEL_ALIAS.items():
        if k.lower() == low:
            return v
    # 模糊：含 voicedesign / -vd 的都算设计
    if "voicedesign" in low or low.endswith("-vd"):
        return MODEL_TTS_VD
    if "voiceclone" in low or low.endswith("-vc"):
        return MODEL_TTS_VC
    return MODEL_TTS

_CONV_CACHE = {}
_CONV_TTL = int(CFG.get("tts", "conversation_ttl", 1800))
_CONV_LOCK = threading.Lock()


def upload_audio(account, data, filename):
    """把音频上传到对象存储，返回可直接读取的 resourceUrl

    ★ audioUrl 必须填 resourceUrl 而不是 uploadUrl ——
      两者 Expires 不同，填错会 500（实测踩过）。

    filename 决定对象名，抓包里 ASR 用 asr-recording.mp3，
    音色克隆参考音频用 voice-clone.mp3（都在 multimedia/{uid}/ 下）。
    """
    r = _mimo_req(account, "POST", "/open-apis/resource/genUploadInfo",
                  body={"fileName": filename}, timeout=30)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(
            f"genUploadInfo 响应不是 JSON（前 120 字符: {resp_text(r)[:120]!r}）")
    d = j.get("data") or {}
    resource_url = d.get("resourceUrl")
    upload_url = d.get("uploadUrl")
    if not (resource_url and upload_url):
        raise RuntimeError(f"genUploadInfo 返回异常: {_fmt_err('genUploadInfo', j)}")

    put_headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)),
    }
    pr = requests.put(upload_url, data=data, headers=put_headers, timeout=120)
    if pr.status_code >= 400:
        raise RuntimeError(
            f"音频上传失败 HTTP {pr.status_code}: {resp_text(pr)[:200]}")
    return resource_url


def ensure_conversation(account):
    """确保有一个已入库(save)的会话，返回 conversationId。
    会话不 save 会导致 TTS/ASR 报 conversation not exist。
    """
    key = str(account.uid or account.user or id(account))
    now = time.time()
    with _CONV_LOCK:
        c = _CONV_CACHE.get(key)
        if c and now - c["ts"] < _CONV_TTL:
            return c["conv"]

    # 新建: chat 一次 -> save 入库
    conv = gen_conv_id()
    payload = {
        "msgId": gen_msg_id(),
        "conversationId": conv,
        "query": "hi",
        "isEditedQuery": False,
        "modelConfig": {"enableThinking": False, "webSearchStatus": "disabled",
                        "model": "mimo-v2.6-pro"},
        "multiMedias": [],
    }
    try:
        call_mimo(account, payload)
    except Exception as e:
        raise RuntimeError(f"建立会话失败: {e}")

    try:
        r = _mimo_req(account, "POST", "/open-apis/chat/conversation/save",
                      body={"conversationId": conv, "title": "API会话", "type": "chat"},
                      timeout=30)
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"会话入库失败: {j}")
    except Exception as e:
        raise RuntimeError(f"会话 save 失败（TTS/ASR 需要入库会话）: {e}")

    with _CONV_LOCK:
        _CONV_CACHE[key] = {"conv": conv, "ts": now}
    return conv


def invalidate_conversation(account):
    key = str(account.uid or account.user or id(account))
    with _CONV_LOCK:
        _CONV_CACHE.pop(key, None)


def _tts_url(account, text, voice=None, scene=None, model=None,
                  reference_audio=None, voice_desc=None, instructions=None):
    """TTS 合成，内部实现：走 save -> generate -> 轮询 -> 刷新签名，返回音频 URL。

    三种模式（协议来自抓包实证）:
      标准      model=mimo-v2.5-tts
                user 位 = 语气/风格描述(可空), audio.voice = 音色名
      音色克隆  model=mimo-v2.5-tts-voiceclone
                user 位 = "", audio.voice = 参考音频的 resourceUrl
                reference_audio 为参考音频二进制（自动上传）
      音色设计  model=mimo-v2.5-tts-voicedesign
                user 位 = 音色描述文字(voice_desc), audio 里**没有** voice

    voice_desc 也可通过 voice 传入（模型为 vd 时 voice 即描述文字）。
    """
    mode = resolve_tts_model(model or CFG.get("tts", "model", MODEL_TTS))
    conv = ensure_conversation(account)
    fmt = CFG.get("tts", "format", "wav")

    audio_cfg = {"format": fmt}
    user_content = ""

    if mode == MODEL_TTS_VD:
        # 音色设计：user 位是音色描述，audio 不带 voice
        desc = voice_desc or voice or ""
        if not desc:
            raise RuntimeError(
                "音色设计(mimo-v2.5-tts-voicedesign)需要音色描述："
                "请传 voice_desc 或把描述文字放进 voice 字段")
        user_content = desc
    elif mode == MODEL_TTS_VC:
        # 音色克隆：先上传参考音频，user 位留空
        if not reference_audio:
            raise RuntimeError(
                "音色克隆(mimo-v2.5-tts-voiceclone)需要参考音频："
                "请传 reference_audio（音频二进制）")
        ref_name = "voice-clone." + sniff_audio_ext(reference_audio, "voice-clone.mp3")
        audio_cfg["voice"] = upload_audio(account, reference_audio, ref_name)
        user_content = ""
    else:
        # 标准：音色名
        v = voice or CFG.get("tts", "voice", "冰糖")
        v = (CFG.get("tts", "voice_map") or {}).get(v, v)
        audio_cfg["voice"] = v
        # 标准模式 user 位是语气/风格描述。
        # instructions 是 OpenAI 标准字段，voice_desc / style 是旧别名。
        user_content = instructions or voice_desc or ""

    # ① 提交任务
    # ★ scene 只在标准模式出现（抓包里 vc/vd 的 modelConfig 只有 modelCode）
    mc = {"modelCode": mode}
    if mode == MODEL_TTS:
        mc["scene"] = scene or CFG.get("tts", "scene", "BRIEF_DESCRIPTION")

    payload = {
        "conversationId": conv,
        "msgId": gen_msg_id(),
        "content": {
            "messages": [
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": text},
            ],
            "audio": audio_cfg,
        },
        "modelConfig": mc,
    }
    r = _mimo_req(account, "POST", "/open-apis/tts/v2/generate", body=payload, timeout=90)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(
            f"tts/v2/generate 响应不是 JSON（前 120 字符: {resp_text(r)[:120]!r}）")
    if j.get("code") != 0:
        # 会话可能失效，清缓存让下次重建
        if "conversation" in str(j.get("msg", "")).lower():
            invalidate_conversation(account)
        raise RuntimeError(_fmt_err("TTS 提交", j))
    task_id = (j.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError(f"TTS 未返回 taskId: {j}")

    # ② 轮询结果（指数退避，短音频不用白等满 1 秒）
    max_wait = int(CFG.get("tts", "max_wait", 120))
    interval = float(CFG.get("tts", "poll_interval", 0.6))
    deadline = time.time() + max_wait
    audio_url, last = None, {}
    while time.time() < deadline:
        pr = _mimo_req(account, "GET",
                       f"/open-apis/tts/generateStatus?taskId={task_id}")
        try:
            pj = pr.json()
        except Exception:
            raise RuntimeError(
                f"generateStatus 响应不是 JSON（前 120 字符: "
                f"{resp_text(pr)[:120]!r}）")
        d = pj.get("data") or {}
        last = d
        st = d.get("status")
        if st == "success":
            audio_url = d.get("audioUrl")
            break
        if st in ("failed", "error"):
            raise RuntimeError(
                f"TTS 任务失败: {json.dumps(d, ensure_ascii=False)[:200]}")
        time.sleep(interval)
        interval = min(interval * 1.4, 2.0)
    if not audio_url:
        raise RuntimeError(
            f"TTS 轮询超时（{max_wait}s，最后状态 {last.get('status')!r}）")

    # ③ 刷新签名（可选，抓包里有这步）
    try:
        gr = _mimo_req(account, "GET", "/open-apis/resource/genNewUrl?oldFileUrl="
                       + urllib.parse.quote(audio_url, safe=""))
        gj = gr.json()
        if gj.get("code") == 0 and gj.get("data"):
            audio_url = gj["data"]
    except Exception:
        pass

    return audio_url


def call_mimo_tts(account, text, voice=None, scene=None, model=None,
                  reference_audio=None, voice_desc=None, instructions=None):
    """TTS 合成，返回音频二进制。走 save -> generate -> 轮询 -> 下载。"""
    audio_url = _tts_url(account, text, voice=voice, scene=scene, model=model,
                         reference_audio=reference_audio,
                         voice_desc=voice_desc, instructions=instructions)
    dr = requests.get(audio_url, timeout=120)
    dr.raise_for_status()
    return dr.content


def tts_task_url(account, text, voice=None, scene=None, model=None,
                 reference_audio=None, voice_desc=None, instructions=None):
    """走完 提交->轮询->刷新签名，返回音频 URL（不下载）

    拆出来是为了流式场景：可以先只拿到 URL，下载时机由调用方决定。
    """
    return _tts_url(account, text, voice=voice, scene=scene, model=model,
                    reference_audio=reference_audio, voice_desc=voice_desc,
                    instructions=instructions)


# ---- 音频格式识别 ----
# 抓包里文件名固定是 asr-recording.mp3，但扩展名应当反映真实格式，
# 否则服务端按错误类型解码会失败或识别质量下降。
# 这里用 magic bytes 判断；认不出来的退回 hint，再退回 mp3。
_AUDIO_SIGS = [
    (b"RIFF", "wav"),      # RIFF....WAVE
    (b"ID3", "mp3"),
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"\xff\xfb", "mp3"),   # MPEG audio frame sync
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
]

_MIME_BY_EXT = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
    "ogg": "audio/ogg", "opus": "audio/ogg", "m4a": "audio/mp4",
    "aac": "audio/aac", "mp4": "audio/mp4", "webm": "audio/webm",
    "amr": "audio/amr", "pcm": "audio/pcm",
}


def sniff_audio_ext(data, hint=None):
    """按 magic bytes 猜音频扩展名；认不出用 hint，再退回 mp3。

    mp3 的帧同步字 0xFFEx 太常见，容易和随机二进制撞上，
    所以只在没有其他更强特征时才认 mp3，且要求第 2 字节高 3 位为 111。
    """
    if not data:
        return (hint or "mp3")
    for sig, ext in _AUDIO_SIGS:
        if data[:len(sig)] == sig:
            # RIFF 还要确认是 WAVE，避免把 AVI/WebP 认成 wav
            if sig == b"RIFF":
                if data[8:12] == b"WAVE":
                    return "wav"
                continue
            if sig[:1] == b"\xff":
                if len(data) > 1 and (data[1] & 0xE0) == 0xE0:
                    return "mp3"
                continue
            return ext
    # ftyp 系（m4a / mp4 / mov），品牌名在偏移 8
    # 12 字节刚好够读到 brand，用 >= 而不是 >（真实 m4a 文件会更长，
    # 但边界情况不该被漏掉）
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"mp42", b"isom", b"mp41"):
            return "m4a"
        return "mp4"
    # EBML（webm / matroska）
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if hint:
        h = str(hint).rsplit(".", 1)[-1].lower().strip()
        if h in _MIME_BY_EXT:
            return h
    return "mp3"


def _fmt_err(where, j):
    """把上游 {code,msg} 包装成可读中文错误"""
    if isinstance(j, dict):
        code = j.get("code")
        msg = j.get("msg") or j.get("message") or ""
        return f"{where} 失败 (code={code} {msg})".strip()
    return f"{where} 失败: {str(j)[:200]}"


def call_mimo_asr(account, audio_bytes, filename=None, language=None,
                  prompt=None):
    """ASR 识别。完整走 genUploadInfo -> PUT -> recognize -> 轮询。

    返回 dict: {"text":..., "taskId":..., "conversationId":..., "raw": {...}}
    （以前只返回字符串，verbose_json / duration 等字段就无从构造）
    """
    conv = ensure_conversation(account)
    cfg_asr = CFG.get("asr") or {}
    lang = language or cfg_asr.get("language") or "auto"
    fname = filename or ("asr-recording." + sniff_audio_ext(audio_bytes, filename))

    # ---- ①② 上传（与音色克隆共用 upload_audio）----
    resource_url = upload_audio(account, audio_bytes, fname)

    # ---- ③ 提交识别 ----
    # audioUrl 必须是 resourceUrl（带签名、可直读），
    # 用 uploadUrl 会得到 500（两者 Expires 不同，已实测踩过坑）。
    body = {
        "conversationId": conv,
        "msgId": gen_msg_id(),
        "audioUrl": resource_url,
        "language": lang,
        "modelConfig": {"modelCode": cfg_asr.get("model", "mimo-v2.5-asr")},
    }
    if prompt:
        body["prompt"] = prompt
    rr = _mimo_req(account, "POST", "/open-apis/asr/recognize", body=body,
                   timeout=60)
    try:
        rj = rr.json()
    except Exception:
        raise RuntimeError(
            f"asr/recognize 响应不是 JSON（前 120 字符: {resp_text(rr)[:120]!r}）")
    if rj.get("code") != 0:
        if "conversation" in str(rj.get("msg", "")).lower():
            invalidate_conversation(account)
        raise RuntimeError(_fmt_err("ASR 提交", rj))
    rd = rj.get("data") or {}
    task_id = rd.get("taskId")
    if not task_id:
        raise RuntimeError(f"ASR 未返回 taskId: {json.dumps(rj)[:200]}")

    # ---- ④ 轮询结果 ----
    # 用指数退避：短音频通常 1~2 秒内出结果，固定 1s 会让每次都白等；
    # 上限 2s，总时长受 max_wait 约束。
    max_wait = int(cfg_asr.get("max_wait", 90))
    interval = float(cfg_asr.get("poll_interval", 0.6))
    deadline = time.time() + max_wait
    last = {}
    while time.time() < deadline:
        sr = _mimo_req(account, "GET",
                       f"/open-apis/asr/recognizeStatus?taskId={task_id}")
        try:
            sj = sr.json()
        except Exception:
            raise RuntimeError(
                f"recognizeStatus 响应不是 JSON（前 120 字符: "
                f"{resp_text(sr)[:120]!r}）")
        sd = sj.get("data") or {}
        last = sd
        st = sd.get("status")
        if st == "success":
            return {"text": sd.get("text") or "", "taskId": task_id,
                    "conversationId": conv, "raw": sd}
        if st in ("failed", "error"):
            raise RuntimeError(
                f"ASR 任务失败: {json.dumps(sd, ensure_ascii=False)[:200]}")
        time.sleep(interval)
        interval = min(interval * 1.4, 2.0)
    raise RuntimeError(
        f"ASR 轮询超时（{max_wait}s，最后状态 "
        f"{last.get('status')!r}）")



# ============================================================
# OpenAI /v1/audio/transcriptions 输出格式兼容
# ============================================================
# OpenAI 支持 json / text / verbose_json / srt / vtt。
# 小米 recognizeStatus 只给 data.text，不含 segments / duration，
# 所以 srt/vtt 只能生成"整段一条"的字幕，verbose_json 里的
# duration / segments 在无数据时如实置空而不编造。


def _fmt_ts(sec):
    """秒 -> SRT 时间戳 00:00:01,234"""
    try:
        sec = float(sec)
    except Exception:
        sec = 0.0
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s_ = sec % 60
    return f"{h:02d}:{m:02d}:{int(s_):02d},{int(round((s_ - int(s_)) * 1000)):03d}"


def _fmt_ts_vtt(sec):
    """秒 -> WebVTT 时间戳 00:00:01.234"""
    return _fmt_ts(sec).replace(",", ".")


def format_asr_output(result, response_format="json", language=None,
                      duration=None):
    """把 call_mimo_asr 的结果转成客户端要的格式。

    result: call_mimo_asr 返回的 dict
    返回 (bytes 负载, content_type)
    """
    fmt = (response_format or "json").lower().strip()
    text = (result or {}).get("text") or ""
    raw = (result or {}).get("raw") or {}

    # 语言：优先用服务端返回的，其次请求/配置里的
    lang = raw.get("language") or language or "auto"

    if fmt == "text":
        return text.encode("utf-8"), "text/plain; charset=utf-8"

    if fmt == "srt":
        # 没有分段信息：整段一条，时间轴 0 -> duration(未知则给个占位)
        end = duration if duration else max(len(text) * 0.08, 1.0)
        body = f"1\n{_fmt_ts(0)} --> {_fmt_ts(end)}\n{text}\n"
        return body.encode("utf-8"), "application/x-subrip; charset=utf-8"

    if fmt == "vtt":
        end = duration if duration else max(len(text) * 0.08, 1.0)
        body = f"WEBVTT\n\n{_fmt_ts_vtt(0)} --> {_fmt_ts_vtt(end)}\n{text}\n"
        return body.encode("utf-8"), "text/vtt; charset=utf-8"

    if fmt == "verbose_json":
        seg = raw.get("segments")
        out = {
            "task": "transcribe",
            "language": lang,
            "duration": raw.get("duration", duration),
            "text": text,
            "segments": seg if seg else [],
        }
        return (json.dumps(out, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8")

    # 默认 json
    out = {"text": text}
    return json.dumps(out, ensure_ascii=False).encode("utf-8"), \
        "application/json; charset=utf-8"


# ============================================================
# 音频格式转换
# ============================================================
# 上游只产出 wav，但 OpenAI 的 response_format 允许
# mp3 / opus / flac / aac。客户端按 Content-Type 解码，
# 声明 mp3 却返回 wav 会播不出来 —— 所以要么真转，要么如实声明。
import shutil as _shutil
import subprocess as _sp

_MIME_AUDIO = {
    "wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac",
    "opus": "audio/opus", "aac": "audio/aac", "ogg": "audio/ogg",
    "m4a": "audio/mp4",
}

_FFMPEG = None


def _find_ffmpeg():
    """找 ffmpeg（Termux 里 pkg install ffmpeg 后就有）"""
    global _FFMPEG
    if _FFMPEG is not None:
        return _FFMPEG or None
    p = CFG.get("tts", "ffmpeg") or ""
    if p and os.path.exists(p):
        _FFMPEG = p
        return p
    p = _shutil.which("ffmpeg")
    _FFMPEG = p or ""
    return p or None


def convert_audio(data, dst_fmt, src_fmt="wav"):
    """把音频转成目标格式。

    返回 (bytes, 实际格式)。转不了就原样返回并如实报告实际格式，
    绝不声明成目标格式却给另一种数据。
    """
    dst = (dst_fmt or "wav").lower().strip()
    if dst in ("", "wav", src_fmt):
        return data, (src_fmt or "wav")

    exe = _find_ffmpeg()
    if not exe:
        # 没有转换器：如实声明为源格式
        return data, (src_fmt or "wav")

    try:
        pr = _sp.run([exe, "-y", "-loglevel", "error",
                      "-f", src_fmt or "wav", "-i", "pipe:0",
                      "-f", dst, "pipe:1"],
                     input=data, stdout=_sp.PIPE, stderr=_sp.PIPE, timeout=120)
        if pr.returncode == 0 and pr.stdout:
            return pr.stdout, dst
        log(f"ffmpeg 转换失败({src_fmt}->{dst}): "
            f"{pr.stderr.decode('utf-8', 'replace')[:120]}")
    except Exception as e:
        log(f"ffmpeg 不可用({src_fmt}->{dst}): {str(e)[:120]}")
    return data, (src_fmt or "wav")


def mime_for(fmt):
    return _MIME_AUDIO.get((fmt or "wav").lower(), "audio/wav")


def list_voices():
    """GET /v1/audio/voices 的音色清单"""
    return {
        "object": "list",
        "data": [
            {"id": v["id"], "name": v["id"], "description": v["desc"],
             "preview_url": v["preview"], "avatar_url": v["avatar"],
             "type": "preset"}
            for v in OFFICIAL_VOICES
        ],
        "modes": [
            {"model": MODEL_TTS, "voice_field": "预设音色名，如 冰糖 / Mia",
             "note": "可配合 instructions(语气) 使用"},
            {"model": MODEL_TTS_VD, "voice_field": "音色描述文字",
             "note": "用自然语言描述想要的音色"},
            {"model": MODEL_TTS_VC, "voice_field": "参考音频 base64 / URL",
             "note": "上传一段参考音频克隆其音色"},
        ],
    }


# ============================================================
# 流式 TTS
# ============================================================
# 上游是异步任务模型：提交 -> 轮询 -> 拿 URL -> 下载。
# 它本身不流式，但可以做两层流式：
#
#   1) 下载流式：拿到音频 URL 后边下边转发（chunked），
#      客户端可以边收边播，不用等整个文件落盘。
#   2) 分段流式：长文本按标点切成多段，每段独立合成，
#      合成完一段立刻推送 —— 长文本首字延迟从"整篇合成完"
#      降到"第一段合成完"，并可并发预取后续段。
#
# wav 是容器格式，多段不能简单首尾相接（会出现 N 个 RIFF 头，
# 播放器只认第一个）。处理办法：
#   - mp3：帧格式，可直接拼接（有 ffmpeg 时优先）
#   - wav：剥离每段头只留 PCM，开头写一个"未知长度"的流式头

def split_text(text, max_chars=180):
    """把长文本切成适合逐段合成的片段。

    优先在句末标点断开，避免把一句话拦腰截断；
    实在没有标点（比如一大串数字）才按长度硬切。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # 句末标点（含中文），后面的引号/括号一起带上
    import re as _re
    seps = _re.compile(r'([。！？!?；;\n]+["」』）)】]*\s*)')
    parts, buf = [], ""
    for seg in seps.split(text):
        if not seg:
            continue
        buf += seg
        if seps.fullmatch(seg):
            # 刚吃到句末标点：可以断
            if buf.strip():
                parts.append(buf)
            buf = ""
        elif len(buf) >= max_chars:
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)

    # 合并过短的相邻片段，避免碎句太多导致语调断裂
    merged = []
    for p in parts:
        if merged and len(merged[-1]) + len(p) <= max_chars // 2:
            merged[-1] += p
        else:
            merged.append(p)

    # 兜底：仍有过长片段则硬切（比如整段无标点）
    out = []
    for p in merged:
        while len(p) > max_chars * 2:
            out.append(p[:max_chars * 2])
            p = p[max_chars * 2:]
        if p:
            out.append(p)
    return out


def parse_wav(data):
    """解析 wav，返回 (fmt_chunk, pcm, info)；非 wav 返回 None

    fmt_chunk 是完整的 'fmt ' 块（含 8 字节头），可用于重写头。
    """
    if not data or len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt_chunk, pcm = 12, None, b""
    info = {}
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt_chunk = data[pos:pos + 8 + size]
            if size >= 16:
                info["channels"] = int.from_bytes(body[2:4], "little")
                info["rate"] = int.from_bytes(body[4:8], "little")
                info["bits"] = int.from_bytes(body[14:16], "little")
        elif cid == b"data":
            pcm += body
        pos += 8 + size + (size & 1)   # chunk 按偶数字节对齐
    if not fmt_chunk:
        return None
    return fmt_chunk, pcm, info


def streaming_wav_header(fmt_chunk):
    """生成一个"长度未知"的 wav 头，用于流式推送

    RIFF size 与 data size 都填 0xFFFFFFFF，播放器按直到 EOF 处理。
    这是 wav 流式播放的常见写法，Chrome / ffplay / 多数解码器都接受。
    """
    hdr = bytearray()
    hdr += b"RIFF" + (0xFFFFFFFF).to_bytes(4, "little") + b"WAVE"
    hdr += fmt_chunk
    hdr += b"data" + (0xFFFFFFFF).to_bytes(4, "little")
    return bytes(hdr)


def iter_download(url, chunk_size=16384, timeout=120):
    """边下载边 yield 音频块"""
    try:
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk
    finally:
        try:
            r.close()
        except Exception:
            pass


def iter_tts_stream(account, text, *, voice=None, model=None,
                    reference_audio=None, voice_desc=None,
                    instructions=None, scene=None,
                    out_fmt="wav", max_chars=180, prefetch=2):
    """流式 TTS 生成器，yield (kind, payload)

    kind:
      "meta"  第一个产出，payload 是 dict（含实际格式/分段数），
              调用方据此发响应头
      "data"  音频字节块
      "end"   结束

    短文本（单段）：拿到 URL 后边下边转发，原样字节，
    wav 头完整，任何播放器都能直接播。

    长文本（多段）：按标点切分，每段独立合成，合成完一段立刻推送。
    多段 wav 不能首尾相接（会有 N 个 RIFF 头），所以剥掉每段头只留
    PCM，开头写一个"长度未知"的流式头。mp3 是帧格式可直接拼接，
    有 ffmpeg 时优先转 mp3。
    """
    from concurrent.futures import ThreadPoolExecutor

    fmt = (out_fmt or "wav").lower()
    parts = split_text(text, max_chars=max_chars)
    if not parts:
        yield ("end", None)
        return

    kw = dict(voice=voice, model=model, reference_audio=reference_audio,
              voice_desc=voice_desc, instructions=instructions, scene=scene)

    if len(parts) == 1:
        # 单段：直接转发下载流，字节原样
        url = tts_task_url(account, parts[0], **kw)
        yield ("meta", {"format": fmt, "segments": 1, "streaming": False})
        for chunk in iter_download(url):
            yield ("data", chunk)
        yield ("end", None)
        return

    # ---- 多段 ----
    # 有 ffmpeg 就能统一转码，此时 mp3 是最省事的流式格式（可直接拼接）
    use_mp3 = fmt == "mp3" and _find_ffmpeg()
    real_fmt = "mp3" if use_mp3 else "wav"
    yield ("meta", {"format": real_fmt, "segments": len(parts),
                    "streaming": True})

    workers = max(1, min(int(prefetch or 1), len(parts)))
    header_written = False

    def synth(seg):
        return tts_task_url(account, seg, **kw)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        # 全部提交，由 worker 数限制并发；按序取结果保证顺序正确
        futs = [ex.submit(synth, seg) for seg in parts]
        for idx, fu in enumerate(futs):
            try:
                url = fu.result()
            except Exception as e:
                # 某段失败：已推出的部分客户端已能播，这里记录后停止
                log(f"TTS 流式第 {idx+1}/{len(parts)} 段失败: {str(e)[:120]}")
                break
            try:
                buf = b"".join(iter_download(url))
            except Exception as e:
                log(f"TTS 流式第 {idx+1}/{len(parts)} 段下载失败: {str(e)[:120]}")
                break

            if use_mp3:
                buf, _ = convert_audio(buf, "mp3", sniff_audio_ext(buf))
                yield ("data", buf)
                continue

            # wav：剥头留 PCM
            parsed = parse_wav(buf)
            if not parsed:
                # 上游没给 wav：有 ffmpeg 就转，否则原样推（尽力而为）
                conv, cf = convert_audio(buf, "wav", sniff_audio_ext(buf))
                parsed = parse_wav(conv)
                if not parsed:
                    if not header_written:
                        yield ("data", streaming_wav_header(b"fmt " + b"\x00" * 16))
                        header_written = True
                    yield ("data", buf)
                    continue

            fmt_chunk, pcm, _info = parsed
            if not header_written:
                yield ("data", streaming_wav_header(fmt_chunk))
                header_written = True
            yield ("data", pcm)

    yield ("end", None)
