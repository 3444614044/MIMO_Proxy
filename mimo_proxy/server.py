# -*- coding: utf-8 -*-
"""
HTTP 服务：路由、请求处理、启动入口
"""

import os
import sys
import traceback
import socket
import select
import json
import time
import base64
import re
import uuid
import hashlib
import threading
import urllib.parse
import http.server
from urllib.parse import urlparse, parse_qs

import requests

from .config import CFG
from .utils import (log, STATS, LOGS, LOG_LOCK, accounts_file,
                    _dec, _enc, gen_msg_id, gen_conv_id, CONVERSATIONS)
from .accounts import POOL, Account

# mimo-claw 容器调度器（claw.auto_schedule=true 时启动）
CLAW_SCHED = None
from .mimo import (
    MODEL_MAP, HEADERS_TPL, upload_media, extract_medias, file_mime,
    build_mimo_payload, parse_sse_events, call_mimo,
    call_mimo_stream, normalize_event, get_account_quota, list_conversations,
    upload_resource, parse_resource, get_user_info, claw_create,
    claw_container, claw_files, claw_conversations,
    claw_dialog, claw_release, claw_quota_all, ensure_container,
    ws_ticket,
    FILE_UPLOAD_TYPES, FILE_MAX_MB, FILE_MAX_COUNT,
)
from .claw_sched import (
    ClawScheduler,
)
from .docxio import generate as docx_generate, parse as docx_parse, sniff as docx_sniff
from .tools import (inject_tools_prompt, extract_tool_calls,
    CLAW_TOOLS, CLAW_TOOL_NAMES, claw_execute, claw_tools_enabled,
                    append_claw_tools,
                    build_tool_catalog, execute_tool,
                    strip_tool_residue, tool_names, tool_schemas,
                    ToolTextGuard, StopFilter)
from .speech import (call_mimo_tts, call_mimo_asr, invalidate_conversation,
                     ensure_conversation, sniff_audio_ext, format_asr_output,
                     resolve_tts_model, MODEL_TTS, MODEL_TTS_VC, MODEL_TTS_VD,
                     OFFICIAL_VOICES, OFFICIAL_VOICE_IDS, classify_voice,
                     V_NAME, V_DESC, V_REF, V_BYTES,
                     convert_audio, mime_for, list_voices,
                     iter_tts_stream)
from .convert import (
    to_openai_chunk, resolve_anthropic_model, anthropic_to_openai_messages,
    anthropic_tools_to_openai, build_anthropic_response,
    responses_tools_to_openai, responses_input_to_messages,
    build_responses_output, resp_event,
)
from .admin import (
    admin_login, admin_check, admin_logout, admin_accounts, _norm_user,
)
from .panel import ADMIN_HTML


# OpenAI 体系里存在、但本代理没有对应上游能力的端点。
# 命中时返回标准错误 JSON 而不是裸 404 —— 见 _not_supported()。
UNSUPPORTED_ENDPOINTS = {
    "/v1/embeddings": (
        "本代理不支持 embeddings。MiMo 上游没有向量接口，"
        "AstrBot 的「知识库」功能需要它 —— 请在 AstrBot 里单独配一个 "
        "embedding 提供商（如 SiliconFlow / 阿里云百炼），"
        "或在 config.json 关闭知识库相关插件。"
    ),
    "/v1/moderations": "本代理不支持内容审核(moderations)。",
    "/v1/images/generations": "本代理不支持图像生成(images)。",
    "/v1/images/edits": "本代理不支持图像编辑(images/edits)。",
    "/v1/rerank": "本代理不支持 rerank。",
    "/v1/completions": (
        "本代理不支持旧版 text completions。"
        "请使用 /v1/chat/completions。"
    ),
}




def _eff_tools(tools):
    """实际注入到提示词里的工具清单 = 客户端 tools + claw 内置工具

    白名单必须与"模型看到的工具"完全一致：claw 工具虽然不是客户端传的，
    但确实被注入了，所以也要算进白名单，否则自动执行提取不到。
    """
    return append_claw_tools(list(tools or [])) or []


def _extract_calls(text, tools):
    """客户端没声明 tools 就**不提取**，让原生协议原样走 content。

    与 _strip_residue 配套：既不提取也不清理，整段透传。
    """
    eff = _eff_tools(tools)
    if not eff:
        return []
    return extract_tool_calls(text, tool_names(eff), tool_schemas(eff))

def _strip_residue(text, tools):
    """只在客户端**真的声明了 tools** 时才清理工具调用残留。

    ★ 关键：deepseek-harness 这类客户端不把 tools 放进请求体，而是用自带的
      system prompt 描述原生 <TOOL_CALL name=".."> 协议。此时模型写出的调用块
      必须**原样透传** —— 一旦清理，客户端解析不到任何工具调用，就会反复报
      "工具调用失败，请重新判断"（模型换多少种写法都没用，因为都被删了）。
    """
    return strip_tool_residue(text) if _eff_tools(tools) else text

def _not_supported(handler, path):
    """未实现但属 OpenAI 体系的端点 -> 标准错误 JSON

    裸 404 + 空 body 时，openai SDK 会把响应解析失败包装成
    JSONDecodeError / APIConnectionError，客户端只看到"连接错误"，
    完全看不出是"这个能力没有"。给出明确 message 能省掉大量排障时间。
    """
    msg = UNSUPPORTED_ENDPOINTS.get(path, "本代理不支持该端点。")
    handler._send_json(404, {
        "error": {
            "message": msg,
            "type": "not_supported",
            "code": "endpoint_not_supported",
            "param": None,
        }
    })


def _not_found(handler, path):
    """完全未知的端点 -> 也返回 JSON，别给空 body"""
    handler._send_json(404, {
        "error": {
            "message": f"未知端点 {path}",
            "type": "invalid_request_error",
            "code": "unknown_endpoint",
            "param": None,
        }
    })


# 对外暴露的模型 ID（config.json 可覆盖 mimo.model_ids）
MODEL_IDS = CFG.get("mimo", "model_ids") or [
    "mimo-v2.6-pro", "mimo-v2.6-flash",
    "mimo-v2.6-pro-ultraspeed-studio",   # 网页端实测模型（fastchat 通道）
    # TTS 三种模式（2026-10-25 抓包实证，见 bot/config 的 redirectTo）
    "mimo-v2.5-tts",                     # 标准：预设音色
    "mimo-v2.5-tts-voiceclone",          # 音色克隆：传参考音频
    "mimo-v2.5-tts-voicedesign",         # 音色设计：传文字描述
    "mimo-v2.5-asr",
]

# 客户端中途断开时 socket 会抛这些异常
_NET_ERRS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# 可打印字符白名单：TLS ClientHello / 二进制协议误连时，请求行会是一串乱码，
# 原样写进日志会把终端搞乱。这里只保留可读字符。
_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " -_./:;?&=+%@~#[]{}()<>,'\"!*"
    "│─┌┐└┘╔╗╚╝║═╠╣╦╩╬▲▼←→↑↓·"
    "⚠✅❌💾📡🔑👥🔄📌⚙🛑🔒"
)


def _safe_log(msg, maxlen=200):
    """把可能含二进制的内容转成可读日志行

    TLS ClientHello 被当请求行时，requestline 会是一串 Latin-1 乱码
    （ü3øÜq£、À+À,Ì©À/À0 之类）。即便前面的 TLS 拦截没生效，
    这里也能保证日志不被二进制污染。
    """
    try:
        text = msg if isinstance(msg, str) else str(msg)
    except Exception:
        text = "<unprintable>"

    # 控制符 / Latin-1 高位字符占比过高 → 判定为二进制，整体折叠
    susp = sum(1 for ch in text
               if (ord(ch) < 0x20 and ch not in "\n\t") or 0x7f <= ord(ch) <= 0x9f
               or 0x00a0 <= ord(ch) <= 0x024f or 0x0400 <= ord(ch) <= 0x04ff)
    if text and susp > len(text) * 0.15:
        return f"<binary {len(text)}B, 疑似TLS/非HTTP流量>"

    out = "".join(ch if ch in _SAFE_CHARS else "." for ch in text)
    out = " ".join(out.split())
    if len(out) > maxlen:
        out = out[:maxlen] + "…"
    return out or "<binary>"


class Handler(http.server.BaseHTTPRequestHandler):
    # 连接/响应状态。必须是类属性：每个请求一个 Handler 实例，
    # 若不初始化，错误处理分支里读 self._client_gone 会抛 AttributeError，
    # 反而把异常处理器本身搞崩（比原始错误更难排查）。
    _client_gone = False     # 客户端已断开
    _started = False         # 响应头已发出
    _chunked = False         # 已切换为 chunked 输出

    # 单线程 HTTPServer 会被一个慢流阻塞全部请求，改为多线程
    def _tls_hint_and_close(self):
        """回一条可读的 JSON 错误，并关闭连接"""
        hint = ("客户端使用了 HTTPS，但本服务只监听 HTTP "
                f"（{CFG.host}:{CFG.port}）。请把客户端 Base URL 改成 "
                "http:// 开头；确实需要 HTTPS 时，在 config.json 的 "
                "server.tls 里配置 cert/key。")
        sys.stderr.write(
            f"[{time.strftime('%H:%M:%S')}] TLS(误用HTTPS) -> {hint}\n")
        try:
            body = json.dumps({"error": {"message": hint}}).encode("utf-8")
            # 不能用 send_response()：它内部会 log_request()，
            # 而 requestline 等属性此时还没被赋值 → AttributeError，
            # 异常又被吞掉，客户端就什么都收不到。裸写状态行最稳。
            self.wfile.write(
                b"HTTP/1.0 400 Bad Request\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True

    def handle_one_request(self):
        """在 socket 层拦截 TLS ClientHello。

        放在这里而不是 parse_request() 里，是因为：
        1. handle_one_request 会先 readline()，TLS 流里没有 \n，
           要等客户端放弃才 EOF，线程被长时间占用；
        2. 用 MSG_PEEK 窥探 socket 首字节，不会消费数据，
           也不依赖 http.server 的内部时序。
        """
        if not getattr(self, "_tls_checked", False):
            self._tls_checked = True
            head = b""
            try:
                r, _, _ = select.select([self.connection], [], [], 0.5)
                if r:
                    head = self.connection.recv(3, socket.MSG_PEEK)
            except Exception:
                head = b""
            if head[:1] == b"\x16" and head[1:2] == b"\x03":
                self._tls_hint_and_close()
                return
        try:
            return super().handle_one_request()
        except Exception:
            return

    def setup(self):
        super().setup()
        # 请求行/头的读取超时。
        # 没有它的话，误用 HTTPS 的客户端（TLS 流里没有 \n）会让
        # readline() 一直挂着，线程被长期占用。
        try:
            t = CFG.get("server", "read_timeout", 30)
            self.connection.settimeout(float(t) if t else None)
        except Exception:
            pass

    def _force_nonstream(self):
        """是否强制走"非流式"路径（内容攒齐后一次性发出）

        部分客户端（AstrBot 等）对流式处理有问题：
        逐字推送的半截标签、工具调用 XML 会先显示出来。
        这里提供三种开法，优先级从高到低：
          1. 请求头 X-No-Stream / X-Force-Nonstream: 1
          2. config.json  server.force_nonstream = true
          3. User-Agent 命中 server.force_nonstream_ua 列表
        注意：仍返回 SSE 封装（客户端 stream=true 需要 SSE 格式），
        只是不再逐字推送 —— 攒齐后用一个 delta 发完。
        """
        for hk in ("X-No-Stream", "X-Force-Nonstream", "X-Mimo-No-Stream"):
            v = (self.headers.get(hk) or "").strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
        if CFG.get("server", "force_nonstream"):
            return True
        ua = (self.headers.get("User-Agent") or "").lower()
        if ua:
            for k in (CFG.get("server", "force_nonstream_ua") or []):
                if str(k).lower() in ua:
                    return True
        return False

    @staticmethod
    @staticmethod
    def _apply_stop(text, stop):
        """按 OpenAI stop 参数截断输出

        上游不支持 stop，客户端（deepseek-harness 等）却可能靠它
        控制生成边界。这里在代理侧软截断 —— 不假装上游支持，
        只保证客户端拿到的内容符合它声明的停止条件。
        """
        if not text or not stop:
            return text, None
        seqs = [stop] if isinstance(stop, str) else list(stop)
        seqs = [x for x in seqs if isinstance(x, str) and x]
        if not seqs:
            return text, None
        best = None
        for sq in seqs:
            i = text.find(sq)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, sq)
        if best is None:
            return text, None
        return text[:best[0]], best[1]

    @staticmethod
    def _upstream_err_text(val):
        """把上游错误事件转成可展示的文本

        ★ 之前三处端点都是 `log(...)` 然后 `continue`，把错误**吞掉**：
          上游繁忙/风控时返回 {"type":"error","content":"服务器繁忙，请稍后再试"}
          或 {"code":xxx,"msg":xxx}，代理只写日志就 continue，
          客户端收到 0 个 chunk + [DONE] -> 整屏空白。

          HAR 实证（2026-10-24）:
            /fastchat/open-apis/chat/dialog/list 返回
            "result":"服务器繁忙，请稍后再试", "dialogStatus":"processing"

          现在把错误内容当正文发给客户端，用户能直接看到原因。
        """
        if isinstance(val, dict):
            for k in ("content", "msg", "message", "result", "error", "text"):
                v = val.get(k)
                if v and isinstance(v, str) and v.strip():
                    code = val.get("code")
                    if code not in (None, 0, "0"):
                        return f"[上游错误 {code}] " + v.strip()
                    return "[上游提示] " + v.strip()
            try:
                import json as _j
                return "[上游错误] " + _j.dumps(val, ensure_ascii=False)[:200]
            except Exception:
                return "[上游错误] 未知"
        return "[上游错误] " + str(val)[:200]

    @staticmethod
    def _tool_call_id(cid, idx):
        """工具调用 id —— 确定性

        之前用 gen_msg_id() 现算：同一请求内每个调用 id 都随机，
        客户端回传 tool_call_id 时无法反查工具名，排障也只能靠猜。
        现在基于本次响应的 cid + 序号，请求内唯一、跨请求可区分。
        """
        base = re.sub(r"[^0-9A-Za-z_]", "", str(cid or ""))[-12:] or "0"
        return f"call_{base}_{idx}"

    @staticmethod
    def _load_audio(raw):
        """把 base64 字符串或 http(s) URL 取成音频二进制"""
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw)
        st = str(raw).strip()
        if st.startswith("http://") or st.startswith("https://"):
            try:
                r = requests.get(st, timeout=60)
                r.raise_for_status()
                return r.content
            except Exception as e:
                raise RuntimeError(f"下载参考音频失败: {e}")
        # base64，可能带 data:audio/xxx;base64, 前缀
        if "," in st and st.split(",", 1)[0].startswith("data:"):
            st = st.split(",", 1)[1]
        try:
            return base64.b64decode(st)
        except Exception as e:
            raise RuntimeError(f"参考音频不是合法 base64 / URL: {e}")

    @staticmethod
    def _fallback_on_empty():
        """正文为空时是否用思考内容兜底

        思考型模型（尤其带工具/MCP 时）有时把答案全部写在 reasoning 里，
        content 为空 -> 客户端显示空白。
        默认开启，可用 mimo.empty_content_fallback=false 关闭。
        """
        return bool(CFG.get("mimo", "empty_content_fallback", True))

    @staticmethod
    def _err_str(e, n=300):
        """把异常转成可安全打印的短字符串

        Cookie 之类的凭证若含二进制，异常信息里会带一整串乱码，
        原样打进日志会把终端搞乱。这里先净化再截断。
        """
        try:
            from .utils import sanitize_cookie_value, has_binary
            txt = str(e)
            # 只有真的含二进制/控制符才追加"凭证"提示。
            # 空格/标点不算 —— 否则 "list index out of range"
            # 会被误报成"凭证含 4 个非法字符"。
            bad = has_binary(txt)
            if bad:
                clean, _ = sanitize_cookie_value(txt)
                return (f"{clean[:n]}" if clean else "") + \
                       f"  <凭证含 {bad} 个非法字符，请重新导入>"
            return txt[:n]
        except Exception:
            try:
                return str(e)[:n]
            except Exception:
                return "<unprintable error>"

    def log_message(self, fmt, *args):
        if " 200 " in str(args):
            return
        msg = (args[0] if args else "")
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {_safe_log(msg)}\n")

    def parse_request(self):
        """兜底：万一 socket 层没探到（数据迟到等），这里再判一次。

        只看 self.raw_requestline（handle_one_request 已读好），
        **不要**在这里 readline() —— 那会把下一行 Header 当成请求行，
        导致所有请求都 400。
        """
        rl = getattr(self, "raw_requestline", b"") or b""
        if rl[:1] == b"\x16" and rl[1:2] == b"\x03":
            self._tls_hint_and_close()
            return False
        return super().parse_request()

    # ---------- 安全写入层 ----------
    # 客户端（rikkahub / LobeChat 等）随时可能断开：用户停止生成、切后台、
    # 超时。此时继续 write 会抛 BrokenPipeError，若错误处理分支再 write，
    # 会连锁炸出一串 traceback。这里统一收敛：一次性标记，后续写入静默跳过。
    def _write(self, data):
        if getattr(self, "_client_gone", False):
            return False
        try:
            self.wfile.write(data)
            return True
        except _NET_ERRS:
            self._client_gone = True
            return False
        except OSError:
            self._client_gone = True
            return False

    def _flush(self):
        if getattr(self, "_client_gone", False):
            return False
        try:
            self.wfile.flush()
            return True
        except _NET_ERRS:
            self._client_gone = True
            return False
        except OSError:
            self._client_gone = True
            return False

    def _sse(self, obj):
        """写一条 SSE；返回 False 表示客户端已断开"""
        ok = self._write(
            f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
        return ok and self._flush()

    def _resp_started(self):
        """响应头是否已发出（此时不能再改发 500）"""
        return getattr(self, "_client_gone", False) or \
            getattr(self, "_started", False)

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        if self._resp_started():
            return
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._started = True
            self.wfile.write(body)
        except _NET_ERRS:
            self._client_gone = True
        except OSError:
            self._client_gone = True

    def _start_chunked(self, code, headers):
        """发一个 chunked 响应头（不带 Content-Length）

        音频流式必须用它：长度事先未知，客户端靠 EOF 判断结束。
        """
        try:
            # 必须 HTTP/1.1 才能用 chunked
            self.protocol_version = "HTTP/1.1"
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self._started = True
            self._chunked = True
            return True
        except _NET_ERRS:
            self._client_gone = True
            return False
        except OSError:
            self._client_gone = True
            return False

    def _chunk(self, data):
        """写一个 chunked 分块；返回 False 表示客户端已断开"""
        if not data:
            return True
        if getattr(self, "_client_gone", False):
            return False
        try:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
            return True
        except _NET_ERRS:
            self._client_gone = True
            return False
        except OSError:
            self._client_gone = True
            return False

    def _chunk_end(self):
        """结束 chunked 响应（终止块）"""
        if getattr(self, "_client_gone", False):
            return
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except _NET_ERRS:
            self._client_gone = True
        except OSError:
            self._client_gone = True

    def _check_auth(self):
        """OpenAI 用 Authorization: Bearer；Anthropic 客户端用 x-api-key，两者都接受"""
        key = None
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
        if key is None:
            key = self.headers.get("x-api-key", "")
        if key != CFG.api_key:
            self._send_json(401, {
                "error": {"message": "Invalid API key",
                          "type": "authentication_error"}})
            return False
        return True

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _read_body(self):
        """按 Content-Type 读请求体，返回 (dict 字段, {文件名: 二进制})

        OpenAI 客户端发 multipart/form-data 是常态（尤其音频接口），
        只认 JSON 会让这些客户端全部失败。Python 3.13+ 已移除 cgi，
        这里自己按 boundary 拆。
        """
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        raw_ct = self.headers.get("Content-Type") or ""
        ct = raw_ct.lower()

        if "multipart/form-data" in ct:
            # ★ 必须传原始 Content-Type：boundary 大小写敏感，
            #   先 lower 会把 ----BOUND 变成 ----bound 而匹配不到分段
            return self._parse_multipart(raw, raw_ct)
        if not raw:
            return {}, {}
        try:
            return json.loads(raw.decode("utf-8")), {}
        except Exception:
            return {}, {}

    @staticmethod
    def _parse_multipart(raw, content_type):
        """极简 multipart/form-data 解析，够用即可"""
        fields, files = {}, {}
        b = "--"
        for part in content_type.split(";"):
            part = part.strip()
            if part.lower().startswith("boundary="):
                b = part.split("=", 1)[1].strip().strip('"')
        if not b:
            return fields, files

        sep = ("--" + b).encode("latin-1")
        for chunk in raw.split(sep):
            chunk = chunk.strip(b"\r\n")
            if not chunk or chunk == b"--":
                continue
            # 头 / 体分界
            idx = chunk.find(b"\r\n\r\n")
            if idx < 0:
                continue
            head, body = chunk[:idx], chunk[idx + 4:]

            name, fname = None, None
            for line in head.decode("utf-8", "replace").split("\r\n"):
                low = line.lower()
                if "content-disposition" in low:
                    for seg in line.split(";"):
                        seg = seg.strip()
                        if seg.lower().startswith("name="):
                            name = seg.split("=", 1)[1].strip().strip('"')
                        elif seg.lower().startswith("filename="):
                            fname = seg.split("=", 1)[1].strip().strip('"')
            if not name:
                continue
            if fname:
                files[name] = body
                fields[name] = fname
            else:
                fields[name] = body.decode("utf-8", "replace")
        return fields, files

    def do_OPTIONS(self):
        self.send_response(200)
        for h, v in [("Access-Control-Allow-Origin", "*"),
                     ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
                     ("Access-Control-Allow-Methods", "GET, POST, OPTIONS")]:
            self.send_header(h, v)
        self.end_headers()

    def _admin_token(self):
        """从请求里提取管理员会话 token（Bearer 或 X-Admin-Token）"""
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Bearer "):
            return hdr[7:].strip()
        return self.headers.get("X-Admin-Token", "").strip()

    def _require_admin(self):
        """优先会话 Token；兼容旧 Basic Auth"""
        tok = self._admin_token()
        if tok and admin_check(tok):
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:]).decode("utf-8")
                user, _, pwd = raw.partition(":")
            except Exception:
                return False
            for a in admin_accounts():
                if _norm_user(a.get("user", "")) == _norm_user(user) \
                        and str(a.get("pass", "")) == pwd:
                    return True
        return False

    def _admin_guard(self):
        if self._require_admin():
            return True
        # 注意: 不发 WWW-Authenticate，避免浏览器弹出原生密码框死循环
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized","needLogin":true}')
        return False

    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/v1/models":
            # 默认不鉴权（方便客户端探测），可在 config.json 里开启
            if CFG.get("auth", "require_for_models", False):
                if not self._check_auth():
                    return
            models = [{"id": m, "object": "model", "owned_by": "mimo"}
                      for m in MODEL_IDS]
            self._send_json(200, {"object": "list", "data": models})
        elif path == "/v1/audio/voices":
            # 音色清单（含官方试听 URL）
            if not self._check_auth():
                return
            self._send_json(200, list_voices())
        elif path == "/health":
            if CFG.get("auth", "require_for_health", False):
                if not self._check_auth():
                    return
            self._send_json(200, {"status": "ok", "accounts": POOL.status()})
        elif path in ("/admin", "/admin/"):
            # 页面本身不做鉴权，由前端登录页自行判断（避免浏览器原生密码框）
            body = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._started = True
            try:
                self.wfile.write(body)
            except (_NET_ERRS, OSError):
                self._client_gone = True
        elif path == "/api/admin/session":
            # 会话校验接口，本身不需要鉴权
            uid = admin_check(self._admin_token())
            self._send_json(200, {"ok": bool(uid), "user": uid})
        elif path == "/api/admin/config":
            if not self._admin_guard():
                return
            who = admin_check(self._admin_token()) or "(basic)"
            self._send_json(200, {
                "apiKey": CFG.api_key,
                "admin": who,
                "models": MODEL_IDS,
                "sessionTtl": int(CFG.get('admin','session_ttl',43200)),
                "autoRefresh": CFG.auto_refresh,
            })
        elif path == "/api/admin/stats":
            if not self._admin_guard():
                return
            st = POOL.status()
            self._send_json(200, {
                "autoRefresh": CFG.auto_refresh,
                "accounts": len(st),
                "valid": sum(1 for a in st if a["valid"]),
                "requests": STATS["requests"],
                "failed": STATS["failed"],
                "uptime": int(time.time() - STATS["start_time"]),
            })
        elif path == "/api/admin/logs":
            if not self._admin_guard():
                return
            with LOG_LOCK:
                self._send_json(200, {"logs": list(LOGS[-80:])})
        elif path == "/api/admin/accounts":
            if not self._admin_guard():
                return
            self._send_json(200, {"accounts": POOL.status()})
        elif path == "/api/admin/quota":
            # 查询每个账号的配额（mimo-claw status + 存储用量）
            if not self._admin_guard():
                return
            self._send_json(200, {"quotas": self._collect_quota()})
        elif path in ("/api/admin/user-info", "/api/admin/debug"):
            # 这两个端点 GET / POST 都支持（脚本习惯用 POST 调用）
            if not self._admin_guard():
                return
            import mimo_proxy.utils as _u
            if path == "/api/admin/user-info":
                self._send_json(200, {"users": self._collect_user_info()})
            else:
                if self.command == "POST":
                    try:
                        b = self._read_json()
                    except Exception:
                        b = {}
                    v = b.get("debug")
                    if v is not None:
                        _u.DEBUG = bool(v)
                self._send_json(200, {"debug": _u.DEBUG, "file": _u.DEBUG_FILE})
        elif path == "/v1/tools":
            if not self._check_auth():
                return
            self._send_json(200, build_tool_catalog())
        elif path == "/api/admin/conversations":
            if not self._admin_guard():
                return
            self._send_json(200, {"conversations": self._collect_conversations()})
        elif path == "/api/admin/claw":
            if not self._admin_guard():
                return
            wait = False
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("wait", [""])[0].lower() in ("1", "true", "yes"):
                wait = True
            self._send_json(200, {"claws": self._collect_claw(wait=wait)})
        elif path == "/api/admin/claw/conversations":
            if not self._admin_guard():
                return
            out = []
            for a in POOL.accounts:
                out.append({"uid": a.uid, "result": claw_conversations(a)})
            self._send_json(200, {"clawConversations": out})
        elif path == "/api/admin/claw/schedule":
            if not self._admin_guard():
                return
            if CLAW_SCHED is None:
                self._send_json(200, {"enabled": False,
                                      "hint": "未启用 claw.auto_schedule"})
                return
            self._send_json(200, CLAW_SCHED.status())
        elif path == "/api/admin/claw/quota":
            if not self._admin_guard():
                return
            self._send_json(200, claw_quota_all())
        elif path == "/api/admin/claw/dialog":
            if not self._admin_guard():
                return
            qs = parse_qs(urlparse(self.path).query)
            cid = qs.get("conversationId", [""])[0]
            uid = qs.get("uid", [""])[0]
            if not cid:
                self._send_json(400, {"error": "缺少 conversationId"})
                return
            if uid:
                sel = [a for a in POOL.accounts if str(a.uid) == uid]
                if not sel:
                    self._send_json(404, {"error": "账号不存在"})
                    return
                self._send_json(
                    200, {"dialog": claw_dialog(sel[0], cid)})
                return
            out = []
            for a in POOL.accounts:
                r = claw_dialog(a, cid)
                if r.get("messages"):
                    out.append({"uid": a.uid, "dialog": r})
            self._send_json(200, {"dialog": out[0]["dialog"]
                                  if out else {"error": "未找到该会话"}})
        elif path == "/api/admin/claw/files":
            if not self._admin_guard():
                return
            qs = parse_qs(urlparse(self.path).query)
            act = qs.get("action", ["list"])[0]
            fpath = qs.get("path", [""])[0]
            uid = qs.get("uid", [""])[0]
            sel = [a for a in POOL.accounts if str(a.uid) == uid] \
                if uid else list(POOL.accounts)
            out = [{"uid": a.uid, "result": claw_files(a, act, fpath)}
                   for a in sel]
            self._send_json(200, {"files": out})
        else:
            _not_found(self, path)
            self.end_headers()

    def do_DELETE(self):
        from urllib.parse import urlparse, parse_qs
        path = urlparse(self.path).path
        if path.startswith("/api/admin/accounts/"):
            if not self._admin_guard():
                return
            try:
                idx = int(path.rsplit("/", 1)[-1])
                POOL.remove(idx)
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
        else:
            _not_found(self, path)

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        # ---- 管理接口（Basic Auth） ----
        if path.startswith("/api/admin/"):
            # 登录/会话校验接口本身不需要鉴权
            if path not in ("/api/admin/login", "/api/admin/session"):
                if not self._admin_guard():
                    return
            self.handle_admin(path)
            return
        elif path == "/v1/tools":
            if not self._check_auth():
                return
            self._send_json(200, build_tool_catalog())
        elif path == "/v1/tools/call":
            if not self._check_auth():
                return
            try:
                b = self._read_json()
            except Exception:
                b = {}
            name = b.get("name") or b.get("tool") or ""
            args = b.get("arguments") or b.get("args") or {}
            if not name:
                self._send_json(400, {"error": {"message": "缺少 name 字段"}})
                return
            r = execute_tool(name, args, uid=b.get("uid"))
            code = 200 if r.get("ok") else 400
            self._send_json(code, r)
        elif path == "/v1/docx/generate":
            if not self._check_auth():
                return
            try:
                b = self._read_json()
            except Exception:
                b = {}
            md = b.get("markdown") or b.get("input") or b.get("text") or ""
            if not str(md).strip():
                self._send_json(400, {"error": {"message":
                    "缺少 markdown / input / text 字段"}})
                return
            title = b.get("title")
            data = docx_generate(str(md), title)
            fn = b.get("filename") or "document.docx"
            if not fn.endswith(".docx"):
                fn += ".docx"
            self.send_response(200)
            self.send_header("Content-Type",
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document")
            self.send_header("Content-Disposition",
                f'attachment; filename="{fn}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._write(data)
        elif path == "/v1/docx/parse":
            if not self._check_auth():
                return
            # multipart 表单 或 直接 POST 原始字节，两种都收
            if "multipart" in (self.headers.get("Content-Type") or "").lower():
                _, files = self._read_body()
                data = b""
                for v in (files or {}).values():
                    if isinstance(v, (bytes, bytearray)) and len(v) > len(data):
                        data = bytes(v)
            else:
                try:
                    ln = int(self.headers.get("Content-Length", 0))
                except Exception:
                    ln = 0
                data = self.rfile.read(ln) if ln else b""
            if not data:
                self._send_json(400, {"error": {"message":
                    "未收到文件：请 POST 原始 docx 字节，或用 multipart 表单 file=@x.docx"}})
                return
            if not docx_sniff(data):
                self._send_json(400, {"error": {"message":
                    "不是 docx 文件（需为 zip 且含 word/document.xml）"}})
                return
            r = docx_parse(data)
            if r.get("error"):
                self._send_json(400, {"error": {"message": r["error"]}})
            else:
                self._send_json(200, r)

        # ---- OpenAI / Anthropic / Responses / TTS / ASR ----
        if path in ("/v1/chat/completions", "/v1/messages", "/v1/responses",
                    "/v1/audio/speech", "/v1/audio/transcriptions"):
            if not self._check_auth():
                return
            # 音频接口客户端常发 multipart/form-data（OpenAI 惯例），
            # 这里统一按 Content-Type 解析
            if path.startswith("/v1/audio/"):
                body, files = self._read_body()
                body = dict(body or {})
                if files:
                    body["_files"] = files
            else:
                body = self._read_json()
            STATS["requests"] += 1
            try:
                if path == "/v1/chat/completions":
                    if body.get("stream"):
                        self.handle_stream(body)
                    else:
                        self.handle_non_stream(body)
                elif path == "/v1/messages":
                    self.handle_anthropic(body)
                elif path == "/v1/responses":
                    self.handle_responses(body)
                elif path == "/v1/audio/speech":
                    self.handle_tts(body)
                elif path == "/v1/audio/transcriptions":
                    self.handle_asr(body)
            except _NET_ERRS:
                # 客户端断开，静默
                self._client_gone = True
                return
            except Exception as e:
                STATS["failed"] += 1
                log(f"ERR {path}: {e}")
                # 记录完整堆栈（最后两帧），否则只剩一行无法定位
                try:
                    frames = traceback.format_exception(type(e), e, e.__traceback__)
                    log("  └ " + " | ".join(
                        f.strip() for f in frames[-3:-1] if f.strip()))
                except Exception:
                    pass
                if not self._resp_started():
                    self._send_json(500, {"error": {"message": self._err_str(e)}})
        elif path in UNSUPPORTED_ENDPOINTS:
            # OpenAI 体系里存在但本代理没有上游能力的端点。
            # 必须返回 OpenAI 标准错误 JSON：
            # 裸 404（无 body）会让 openai SDK 抛出难以理解的解析异常，
            # 客户端还以为是网络问题。
            _not_supported(self, path)
        else:
            _not_found(self, path)

    # ---------- TTS ----------
    def handle_tts(self, req):
        """POST /v1/audio/speech —— 三种模式只靠 model + voice 区分

        一般 API 约定（无需任何私有字段）:

          model = mimo-v2.5-tts             voice = 预设音色名，如 冰糖 / Mia
          model = mimo-v2.5-tts-voicedesign voice = 音色描述文字
          model = mimo-v2.5-tts-voiceclone  voice = 参考音频(base64 / data URI / URL)

        同时兼容 OpenAI 标准字段：input / response_format / instructions /
        speed(接受但上游不支持)。旧私有字段 reference_audio / voice_desc
        继续可用。
        """
        files = req.get("_files") or {}
        text = req.get("input") or req.get("text") or ""
        if not text:
            self._send_json(400, {"error": {"message": "缺少 input 文本"}})
            return

        model = req.get("model") or CFG.get("tts", "model", MODEL_TTS)
        mode = resolve_tts_model(model)

        # voice 可以从 multipart 文件里来（form 上传参考音频）
        voice = req.get("voice")
        if files.get("voice"):
            voice = files["voice"]
        if voice is None:
            voice = CFG.get("tts", "voice", "冰糖")

        # 语气/风格描述：OpenAI 标准的 instructions，或旧的 style / voice_desc
        instructions = (req.get("instructions") or req.get("voice_desc")
                        or req.get("style") or "")

        kind, payload = classify_voice(voice, mode)

        ref_bytes, voice_name, voice_desc = None, None, None
        if mode == MODEL_TTS_VC:
            # 音色克隆：voice 必须是音频。
            # reference_audio / reference 是早期私有字段，继续兼容。
            legacy = req.get("reference_audio") or req.get("reference")
            if kind == V_BYTES:
                ref_bytes = payload
            elif kind == V_REF:
                ref_bytes = self._load_audio(payload)
            elif legacy:
                ref_bytes = self._load_audio(legacy)
            else:
                self._send_json(400, {"error": {"message": (
                    "音色克隆(mimo-v2.5-tts-voiceclone)的 voice 必须是参考音频："
                    "base64 / data URI / http URL / 表单文件上传；"
                    f"当前收到的是{'预设音色名' if kind == V_NAME else '文字'}"
                    f"（{str(voice)[:40]}）")}})
                return
        elif mode == MODEL_TTS_VD:
            # 音色设计：voice 就是描述文字
            voice_desc = (payload if kind == V_DESC else None) or instructions \
                or (voice if isinstance(voice, str) else "") or ""
            if not voice_desc:
                self._send_json(400, {"error": {"message": (
                    "音色设计(mimo-v2.5-tts-voicedesign)需要音色描述："
                    "把描述文字放进 voice 或 instructions 字段")}})
                return
        else:
            # 标准：voice 是音色名，instructions 是语气
            if kind == V_NAME:
                voice_name = payload
            elif kind in (V_DESC, None):
                # 不是已知音色名时，按名字原样传（上游可能新增音色）
                voice_name = voice
            else:
                # 传了音频/URL：提醒换模型，别默默当音色名
                self._send_json(400, {"error": {"message": (
                    f"voice 看起来是音频引用，但 model={model} 是标准合成。"
                    "要用参考音频请改 model=mimo-v2.5-tts-voiceclone")}})
                return

        # speed 是 OpenAI 标准字段，上游没有对应能力：接受但不假装支持
        if req.get("speed") not in (None, "", 1, 1.0):
            log("TTS: 收到 speed 参数，上游不支持，已忽略")

        out_fmt = (req.get("response_format")
                   or CFG.get("tts", "format", "wav")).lower().strip()
        if out_fmt in ("", "mp3", "wav", "flac", "opus", "aac", "ogg", "m4a"):
            pass
        else:
            self._send_json(400, {"error": {"message": (
                f"不支持的 response_format: {out_fmt}"
                "（可选 wav/mp3/flac/opus/aac/ogg/m4a）")}})
            return

        # ---- 流式输出 ----
        # stream=true（或 OpenAI 的 stream_format）时改用 chunked 推送，
        # 客户端可以边收边播，长文本还能分段边合成边推。
        want_stream = req.get("stream")
        if want_stream is None:
            want_stream = req.get("stream_format")
        if isinstance(want_stream, str):
            want_stream = want_stream.strip().lower() not in ("", "false", "0", "none")
        want_stream = bool(want_stream)
        if want_stream:
            self._handle_tts_stream(req, dict(
                text=text, model=model, voice_name=voice_name,
                ref_bytes=ref_bytes, voice_desc=voice_desc,
                instructions=instructions, out_fmt=out_fmt, mode=mode))
            return

        for attempt in range(2):
            account = POOL.get_next()
            if not account or not account.is_valid():
                break
            try:
                audio = call_mimo_tts(account, text, voice=voice_name,
                                      model=model, reference_audio=ref_bytes,
                                      voice_desc=voice_desc,
                                      instructions=instructions)
                audio, real_fmt = convert_audio(audio, out_fmt, "wav")
                if real_fmt != (out_fmt or "wav"):
                    # 转不了就如实声明，别让客户端按错格式解码
                    log(f"TTS: {out_fmt} 不可用，回退 {real_fmt}")
                self.send_response(200)
                self.send_header("Content-Type", mime_for(real_fmt))
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("X-Mimo-Tts-Mode", mode)
                self.send_header("X-Audio-Format", real_fmt)
                self.end_headers()
                self.wfile.write(audio)
                log(f"TTS ok model={mode} fmt={real_fmt} bytes={len(audio)}")
                return
            except Exception as e:
                log(f"TTS 失败(尝试{attempt+1}): {str(e)[:150]}")
                if not isinstance(e, _NET_ERRS):
                    account.mark_fail(e)
                invalidate_conversation(account)
                if attempt == 0:
                    continue
        self._send_json(503, {"error": {"message": "TTS 不可用，请检查账号与会话"}})

    def _handle_tts_stream(self, req, p):
        """流式 TTS：chunked 推送，边合成边发"""
        account = POOL.get_next()
        if not account or not account.is_valid():
            self._send_json(503, {"error": {"message": "没有可用账号"}})
            return

        cfg = CFG.get("tts") or {}
        max_chars = int(cfg.get("stream_max_chars", 180))
        prefetch = int(cfg.get("stream_prefetch", 2))

        it = iter_tts_stream(
            account, p["text"], voice=p["voice_name"], model=p["model"],
            reference_audio=p["ref_bytes"], voice_desc=p["voice_desc"],
            instructions=p["instructions"], out_fmt=p["out_fmt"],
            max_chars=max_chars, prefetch=prefetch)

        try:
            kind, meta = next(it)
        except StopIteration:
            self._send_json(503, {"error": {"message": "TTS 流式无输出"}})
            return
        except Exception as e:
            # 还没写响应头，可以正常报错
            invalidate_conversation(account)
            if not isinstance(e, _NET_ERRS):
                account.mark_fail(e)
            self._send_json(502, {"error": {"message": f"TTS 流式失败: {e}"}})
            return

        meta = meta or {}
        real_fmt = meta.get("format") or p["out_fmt"] or "wav"
        segs = meta.get("segments", 1)
        if not self._start_chunked(200, [
                ("Content-Type", mime_for(real_fmt)),
                ("X-Mimo-Tts-Mode", p["mode"]),
                ("X-Audio-Format", real_fmt),
                ("X-Mimo-Tts-Segments", str(segs)),
        ]):
            return

        total = 0
        ok = True
        try:
            for kind, payload in it:
                if kind == "data":
                    if not self._chunk(payload):
                        ok = False
                        break
                    total += len(payload)
                elif kind == "end":
                    break
        except Exception as e:
            log(f"TTS 流式中途失败: {str(e)[:150]}")
            ok = False

        if ok:
            self._chunk_end()
            log(f"TTS 流式完成 mode={p['mode']} fmt={real_fmt} "
                f"segments={segs} bytes={total}")
        # 客户端断开不是账号故障，不 mark_fail

    # ---------- ASR ----------
    def handle_asr(self, req):
        """POST /v1/audio/transcriptions

        兼容 OpenAI: file(base64 / data: URL / http URL)、language、
        prompt、response_format(json|text|verbose_json|srt|vtt)。
        """
        # multipart 上传的文件优先（OpenAI 惯例：file 字段）
        files = req.get("_files") or {}
        src = req.get("file") or req.get("audio") or req.get("url") or ""
        if files.get("file") or files.get("audio"):
            audio = files.get("file") or files.get("audio")
        elif not src:
            self._send_json(400, {"error": {"message": "缺少音频（file 字段，base64 或 URL）"}})
            return
        else:
            audio = None
        if audio is None:
          try:
            if src.startswith("http://") or src.startswith("https://"):
                rr = requests.get(src, timeout=120)
                rr.raise_for_status()
                audio = rr.content
            else:
                if "," in src and src.strip().startswith("data:"):
                    src = src.split(",", 1)[1]
                audio = base64.b64decode(src)
          except Exception as e:
            self._send_json(400, {"error": {"message": f"音频解码失败: {e}"}})
            return

        if not audio:
            self._send_json(400, {"error": {"message": "音频内容为空"}})
            return

        cfg_asr = CFG.get("asr") or {}
        max_bytes = int(cfg_asr.get("max_bytes", 10 * 1024 * 1024))
        if len(audio) > max_bytes:
            mb = max_bytes / 1024 / 1024
            self._send_json(400, {"error": {
                "message": f"音频 {len(audio) / 1024 / 1024:.1f}MB 超过 {mb:.0f}MB 限制"
                f"（可调 config.json 的 asr.max_bytes）"}})
            return

        # 扩展名：优先用客户端给的文件名，其次按 magic bytes 嗅探
        hint = req.get("filename") or req.get("file_name") or None
        ext = sniff_audio_ext(audio, hint)
        filename = f"asr-recording.{ext}"

        # 语言 / 提示词：请求级优先于配置
        language = req.get("language") or cfg_asr.get("language") or "auto"
        prompt = req.get("prompt") or None
        rfmt = req.get("response_format") or "json"

        supported = ("json", "text", "verbose_json", "srt", "vtt")
        if rfmt not in supported:
            self._send_json(400, {"error": {
                "message": f"不支持的 response_format: {rfmt}（可选 {', '.join(supported)}）"}})
            return

        last_err = None
        for attempt in range(2):
            account = POOL.get_next()
            if not account or not account.is_valid():
                break
            try:
                result = call_mimo_asr(account, audio, filename,
                                       language=language, prompt=prompt)
                payload, ctype = format_asr_output(result, rfmt, language)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            except Exception as e:
                last_err = e
                log(f"ASR 失败(尝试{attempt+1}): {str(e)[:150]}")
                if not isinstance(e, _NET_ERRS):
                    account.mark_fail(e)
                invalidate_conversation(account)
                if attempt == 0:
                    continue
        msg = str(last_err) if last_err else "无可用账号"
        self._send_json(503, {"error": {"message": f"ASR 不可用: {msg[:200]}"}})

    # ---------- 管理接口 ----------
    def handle_admin(self, path):
        # 与 do_GET 共用同一批查询端点（POST 调用也能查）
        if path in ("/api/admin/user-info", "/api/admin/debug"):
            if not self._admin_guard():
                return
            import mimo_proxy.utils as _u
            if path == "/api/admin/user-info":
                self._send_json(200, {"users": self._collect_user_info()})
                return
            try:
                b = self._read_json()
            except Exception:
                b = {}
            v = b.get("debug")
            if v is not None:
                _u.DEBUG = bool(v)
            self._send_json(200, {"debug": _u.DEBUG, "file": _u.DEBUG_FILE})
            return
        # ---- 登录 / 登出 / 会话 ----
        if path == "/api/admin/login":
            body = self._read_json()
            user = (body.get("user") or body.get("username")
                    or body.get("email") or "").strip()
            pwd = body.get("pass") or body.get("password") or ""
            if not user or not pwd:
                self._send_json(400, {"error": "请填写用户名和密码"})
                return
            tok, ttl = admin_login(user, pwd)
            if not tok:
                self._send_json(401, {"error": "用户名或密码错误"})
                return
            self._send_json(200, {"ok": True, "token": tok, "user": user, "ttl": ttl})
            return

        if path == "/api/admin/logout":
            admin_logout(self._admin_token())
            self._send_json(200, {"ok": True})
            return

        if path == "/api/admin/session":
            uid = admin_check(self._admin_token())
            self._send_json(200, {"ok": bool(uid), "user": uid})
            return

        # ---- 登录后可见的配置（含 API Key，供面板自测用） ----
        if path == "/api/admin/config":
            who = admin_check(self._admin_token()) or "(basic)"
            self._send_json(200, {
                "apiKey": CFG.api_key,
                "admin": who,
                "models": MODEL_IDS,
                "sessionTtl": int(CFG.get('admin','session_ttl',43200)),
                "autoRefresh": CFG.auto_refresh,
            })
            return

        if path == "/api/admin/accounts":
            body = self._read_json()

            # ---- 模式 A: 小米账号 + 密码（自动登录拿 token）----
            user = (body.get("user") or "").strip()
            pwd = body.get("pass") or ""
            if user and pwd:
                ph = (body.get("ph") or "").strip() or None
                acc, ok, err = POOL.add_credential(user, pwd, ph)
                log(f"账密添加 {user} -> {'成功' if ok else '失败: ' + str(err)}")
                self._send_json(200, {
                    "ok": ok,
                    "uid": acc.uid,
                    "user": acc.user,
                    "valid": acc.is_valid(),
                    # 登录成功但缺 ph 时提示用户补填
                    "needPh": bool(acc.token) and not acc.ph,
                    "error": err,
                })
                return

            # ---- 模式 C: 直接粘贴 accounts.json 内容（支持加密/明文，可批量）----
            raw_items = body.get("accounts")
            if isinstance(raw_items, list) and raw_items:
                added, failed = [], []
                for it in raw_items:
                    if not isinstance(it, dict):
                        continue
                    uid = str(it.get("uid") or "")
                    # 加密字段优先，其次明文字段
                    ph = _dec(it.get("ph_enc", "")) or (it.get("ph") or "")
                    tk = _dec(it.get("token_enc", "")) or (it.get("token") or "")
                    pw = _dec(it.get("pass_enc", "")) or (it.get("pass") or "")
                    user = it.get("user")
                    try:
                        if user and pw:
                            acc, ok, err = POOL.add_credential(user, pw, ph or None)
                            added.append({"uid": acc.uid, "user": acc.user,
                                          "valid": acc.is_valid(),
                                          "needPh": bool(acc.token) and not acc.ph})
                        elif uid and tk and ph:
                            acc = POOL.add_or_update(uid, tk, ph)
                            ok = POOL.test_account(acc)
                            added.append({"uid": acc.uid, "valid": ok})
                        else:
                            failed.append({"uid": uid, "error": "字段不完整"})
                    except Exception as e:
                        failed.append({"uid": uid, "error": str(e)[:120]})
                POOL.save()
                log(f"批量导入账号: 成功 {len(added)} 失败 {len(failed)}")
                # 解密失败的通常原因是密文来自另一台机器（密钥不同）
                hint = ""
                if failed:
                    joined = " ".join(str(f.get("error", "")) for f in failed)
                    looks_enc = any(
                        str(it.get("ph_enc", "")).startswith("enc1:")
                        or str(it.get("token_enc", "")).startswith("enc1:")
                        for it in raw_items if isinstance(it, dict))
                    if looks_enc and ("字段不完整" in joined or not added):
                        hint = ("加密字段解不开，多半是密文来自另一台机器"
                                "（.secret_key 不同），请改用明文 token 或 Cookie 串导入")
                    elif joined.strip():
                        hint = "部分账号处理出错，详见失败明细"
                self._send_json(200, {"ok": bool(added), "added": added,
                                      "failed": failed, "hint": hint})
                return

            # ---- 模式 B: Cookie 串（支持多行，一次导多个账号）----
            cookie = body.get("cookie", "")
            if not cookie:
                self._send_json(400, {"error": "缺少 cookie 或 user/pass"})
                return

            # ★ 多行支持：按换行/分号切分成若干"一行一个账号"。
            #   之前直接把整段丢给 re.search：
            #     1) [^;]+ 不含换行限制 -> 最后一字段会把下一行内容吞进来
            #        例: ph 变成 'PH1=="\nserviceToken=TOK456'
            #     2) search 只取第一个匹配 -> 第二行起的账号全部丢失
            #   所以先按行拆，再逐行解析。
            raw_lines = [ln.strip() for ln in re.split(r"[\r\n]+", cookie)
                         if ln.strip()]
            # 兼容用分号分隔多账号（少见但支持）
            parsed, bad = [], []
            for ln in raw_lines:
                def field(k, _ln=ln):
                    # [^;\n]+ —— 必须排除换行，否则跨行污染
                    m = re.search(rf"{k}=([^;\n]+)", _ln)
                    return m.group(1).strip().strip('"') if m else ""

                token = (field("serviceToken")
                         or field("xiaomichatbot_serviceToken"))
                uid = field("userId")
                ph = field("xiaomichatbot_ph")
                if token and uid and ph:
                    parsed.append((uid, token, ph))
                else:
                    bad.append({
                        "line": ln[:60],
                        "error": "解析失败，需 serviceToken/userId/xiaomichatbot_ph",
                    })

            if not parsed:
                self._send_json(400, {
                    "error": "解析失败，需 serviceToken/userId/xiaomichatbot_ph",
                    "failed": bad[:5]})
                return

            added, failed = [], []
            for uid, token, ph in parsed:
                acc = POOL.add_or_update(uid, token, ph)
                try:
                    ok = POOL.test_account(acc)
                    err = None
                except Exception as te:      # noqa: BLE001
                    ok = False
                    err = f"{type(te).__name__}: {te}"
                    log("导入后测试异常:\n" + "".join(
                        traceback.format_exception(type(te), te, te.__traceback__)))
                log(f"导入账号 {uid} -> {'可用' if ok else '不可用'}")
                added.append({"uid": acc.uid, "ok": ok, "error": err})
            failed.extend(bad)
            POOL.save()
            self._send_json(200, {"ok": any(a["ok"] for a in added),
                                  "added": added, "failed": failed,
                                  "uid": added[0]["uid"] if added else None,
                                  "error": added[0]["error"] if added else None})
        elif path == "/api/admin/claw/create":
            if not self._admin_guard():
                return
            try:
                b = self._read_json()
            except Exception:
                b = {}
            uid = str(b.get("uid", "") or "")
            wait = bool(b.get("wait", True))
            sel = [a for a in POOL.accounts if str(a.uid) == uid] \
                if uid else list(POOL.accounts)
            if not sel:
                self._send_json(404, {"error": "账号不存在"})
                return
            out = []
            for a in sel:
                res = claw_create(a)
                if wait and not res.get("error"):
                    res["container"] = claw_container(a, wait_available=True)
                out.append({"uid": a.uid, "create": res})
            self._send_json(200, {"results": out})
        elif path == "/api/admin/claw/schedule/run":
            if not self._admin_guard():
                return
            if CLAW_SCHED is None:
                self._send_json(400, {"error": "未启用 claw.auto_schedule"})
                return
            try:
                b = self._read_json()
            except Exception:
                b = {}
            r = CLAW_SCHED.tick(uid=b.get("uid"),
                                ignore_limit=bool(b.get("ignore_limit")))
            self._send_json(200, r)
        elif path == "/api/admin/claw/ensure":
            if not self._admin_guard():
                return
            try:
                b = self._read_json()
            except Exception:
                b = {}
            uid = str(b.get("uid", "") or "")
            auto = bool(b.get("auto_create", True))
            sel = [a for a in POOL.accounts if str(a.uid) == uid] \
                if uid else list(POOL.accounts)
            out = [{"uid": a.uid, "container": ensure_container(
                        a, auto_create=auto)}
                   for a in sel]
            self._send_json(200, {"results": out})
        elif path == "/api/admin/claw/release":
            if not self._admin_guard():
                return
            try:
                b = self._read_json()
            except Exception:
                b = {}
            uid = str(b.get("uid", "") or "")
            sel = [a for a in POOL.accounts if str(a.uid) == uid] \
                if uid else list(POOL.accounts)
            out = [{"uid": a.uid, "release": claw_release(a)} for a in sel]
            self._send_json(200, {"results": out})
        elif path == "/api/admin/claw/ws":
            if not self._admin_guard():
                return
            qs = parse_qs(urlparse(self.path).query)
            uid = qs.get("uid", [""])[0]
            sel = [a for a in POOL.accounts if str(a.uid) == uid] \
                if uid else list(POOL.accounts)
            out = [{"uid": a.uid, "ws": ws_ticket(a)} for a in sel]
            self._send_json(200, {"results": out})
        elif re.match(r"^/api/admin/accounts/\d+/test$", path):
            parts = path.split("/")
            # 正则已保证段数，但多一层防御：段数不足时不抛 IndexError
            idx = int(parts[4]) if len(parts) > 4 else -1
            if idx < 0:
                self._send_json(400, {"error": "路径格式不正确"})
                return
            try:
                acc = POOL.accounts[idx]
                ok = POOL.test_account(acc)
                # ok 是真实测试结果，不能硬编码成 True，
                # 否则面板上"测试"永远显示成功，账号其实已经失效。
                self._send_json(200, {"ok": bool(ok), "error": None})
            except Exception as e:
                self._send_json(400, {"error": str(e)})
        else:
            _not_found(self, path)

    # ---------- claw 内置工具自动执行 ----------
    def _claw_auto_loop(self, account, req, tools, model, conv_id,
                        tool_calls, extra_rounds=None):
        """代理侧自动执行 claw 内置工具，把结果回灌给上游

        客户端（dsh / AstrBot）并没有实现这些工具，若代理只是把
        tool_calls 抛回去，客户端执行失败 -> 整轮对话白费。
        开启 claw.auto_execute 后由代理自己跑完整个循环，
        客户端拿到的就是最终答案。

        返回 (text, reasoning, rounds)；未启用或无 claw 调用返回 None。
        """
        import mimo_proxy.config as _c
        if not _c.CFG.get("claw", "auto_execute"):
            return None
        names = [tc.get("name") for tc in tool_calls or []]
        if not names or not all(n in CLAW_TOOL_NAMES for n in names):
            return None

        max_rounds = int(_c.CFG.get("claw", "auto_execute_rounds", 3) or 3)
        messages = list(req.get("messages") or [])
        reasoning_all = []
        rounds = 0

        for _ in range(max_rounds):
            rounds += 1
            results = []
            for tc in tool_calls:
                nm = tc.get("name")
                args = tc.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                out = claw_execute(account, nm, args)
                results.append(f"[工具结果] {nm}: {out}")
            if not results:
                break
            # 把本轮调用与结果作为 user 消息回灌
            messages = messages + [
                {"role": "user", "content": "\n".join(results)}]
            try:
                payload, mapped = build_mimo_payload(
                    messages, model, tools=tools,
                    reasoning_effort=req.get("reasoning_effort", "default"),
                    conversation_id=conv_id,
                    history_mode=CFG.get("mimo", "history_mode", "full"),
                    temperature=req.get("temperature"),
                    top_p=req.get("top_p"),
                    web_search=self._web_search(req),
                    tool_choice=req.get("tool_choice"),
                    parallel_tool_calls=req.get("parallel_tool_calls", True))
                raw = call_mimo(account, payload)
                events = parse_sse_events(raw)
            except Exception as e:      # noqa: BLE001
                return ("执行 claw 工具后重新请求失败: " + str(e)[:200],
                        "", rounds)

            text_parts, reason_parts = [], []
            for _evt, data in events:
                if not isinstance(data, dict):
                    continue
                for kind, val in normalize_event(data):
                    if kind == "reasoning" and val:
                        reason_parts.append(val)
                    elif kind == "content" and val:
                        text_parts.append(val)
            full = "".join(text_parts)
            reasoning_all.append("".join(reason_parts))
            nxt = _extract_calls(full, tools)
            next_names = [t.get("name") for t in nxt or []]
            if nxt and all(n in CLAW_TOOL_NAMES for n in next_names):
                # 继续下一轮
                tool_calls = nxt
                continue
            clean = _strip_residue(full, tools)
            return (clean, "".join(reasoning_all), rounds)

        return ("claw 工具自动执行达到轮数上限（%d 轮）" % rounds,
                "".join(reasoning_all), rounds)

    # ---------- 流式 ----------
    def handle_stream(self, req):
        account = POOL.get_next()
        if not account or not account.is_valid():
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"no valid mimo account"}}')
            return

        model = req.get("model", "mimo-v2.6-pro")
        messages = req.get("messages", [])
        tools = req.get("tools")
        reasoning_effort = req.get("reasoning_effort", "default")

        multi_medias = self._upload_medias(account, messages, req)

        conv_id = self._conversation_id(req)
        payload, mapped = build_mimo_payload(
            messages, model, tools=tools,
            reasoning_effort=reasoning_effort,
            conversation_id=conv_id,
            history_mode=CFG.get("mimo", "history_mode", "full"),
            temperature=req.get("temperature"),
            top_p=req.get("top_p"),
            web_search=self._web_search(req),
            tool_choice=req.get("tool_choice"),
            parallel_tool_calls=req.get("parallel_tool_calls", True))
        payload["multiMedias"] = multi_medias
        # 复用会话：首次生成后记住，后续同一会话沿用
        CONVERSATIONS.put(self._conv_key(req), payload["conversationId"])

        # ★ 强制非流式：先判，header 要在 end_headers 之前发
        oneshot = self._force_nonstream()

        # SSE 无 Content-Length，客户端靠连接关闭判断结束
        self.close_connection = True
        self.send_response(200)
        for h, v in [("Content-Type", "text/event-stream"),
                     ("Cache-Control", "no-cache"),
                     ("Connection", "close"),
                     ("Access-Control-Allow-Origin", "*")]:
            self.send_header(h, v)
        if oneshot:
            self.send_header("X-Mimo-Stream", "oneshot")
        self.end_headers()
        self._started = True

        # 一次请求内固定的 id / created
        created = int(time.time())
        cid = f"chatcmpl-{gen_msg_id()[:16]}"

        full_text = []
        reasoning_parts = []
        usage = None
        sent_reasoning = False
        sent_content = False
        seq = 0
        guard = ToolTextGuard(enabled=bool(_eff_tools(tools)))
        sf = StopFilter(req.get("stop"))

        try:
            # 真流式：边收上游边转发，不再等上游全部结束
            for _evt, data in call_mimo_stream(account, payload):
                if self._client_gone:
                    return
                # 心跳：上游静默期间发注释行，防止中间设备/客户端超时断开
                if _evt == "__heartbeat__":
                    if not self._write(b": ping\n\n"):
                        return
                    self._flush()
                    continue
                # 一个事件可能同时含 reasoning 和 content，逐个处理，
                # 只取一个会丢正文（表现：只有思考没有回答）
                items = normalize_event(data)
                for kind, val in items:
                    if kind == "usage":
                        usage = val
                        continue
                    if kind == "error":
                        # ★ 不再 continue 吞掉：转成正文发给客户端。
                        #   否则上游繁忙/风控时客户端整屏空白且不知原因。
                        #   HAR 实证: "result":"服务器繁忙，请稍后再试"
                        etext = self._upstream_err_text(val)
                        log(f"上游错误事件: {etext[:150]}")
                        seq += 1
                        self._sse(to_openai_chunk(
                            content=etext, model=mapped, cid=cid,
                            created=created))
                        continue
                    if not val:
                        continue

                    seq += 1
                    if kind == "reasoning":
                        reasoning_parts.append(val)
                        sent_reasoning = True
                        if oneshot:
                            continue
                        if not self._sse(to_openai_chunk(
                                reasoning=val, model=mapped, cid=cid,
                                created=created)):
                            return
                    elif kind == "content":
                        # ★ stop 必须先于发送过滤：流式是边收边发的，
                        #   等收完再截断，内容早就发给客户端了。
                        if sf.active():
                            val = sf.feed(val)
                            if not val:
                                continue
                        full_text.append(val)
                        sent_content = True
                        if oneshot:
                            continue
                        # ★ 疑似工具调用的片段先压住，别把 XML 念给用户
                        safe = guard.feed(val)
                        if safe and not self._sse(to_openai_chunk(
                                content=safe, model=mapped, cid=cid,
                                created=created)):
                            return

            reason_joined = "".join(reasoning_parts)
            joined = "".join(full_text)
            # StopFilter 压住的尾部若没构成完整 stop，补发出来
            if sf.active():
                rest_stop = sf.drain()
                if rest_stop:
                    full_text.append(rest_stop)
                    joined += rest_stop
                    if not oneshot:
                        self._sse(to_openai_chunk(
                            content=rest_stop, model=mapped, cid=cid,
                            created=created))
            # ★ 流式空正文兜底：全程只有 reasoning 没有 content 时，
            #   补发一段思考内容，避免客户端整屏空白。
            if not joined.strip() and reason_joined.strip() \
                    and self._fallback_on_empty():
                joined = reason_joined.strip()
                self._sse(to_openai_chunk(
                    content=joined, model=mapped, cid=cid, created=created))
            # ★ 白名单校验: 只有客户端提供的工具名才算调用。
            #   否则正文里的 JSON / 复述的调用格式会被误判成工具调用，
            #   rikkahub 去执行不存在的 MCP 工具 -> 前端空白。
            tool_calls = _extract_calls(joined, tools)
            if oneshot:
                # 攒齐后一次性发：思考一段 + 正文一段，之后按原逻辑发
                # tool_calls / finish_reason / [DONE]。
                if reason_joined.strip():
                    self._sse(to_openai_chunk(
                        reasoning=reason_joined, model=mapped, cid=cid,
                        created=created))
                _out = _strip_residue(joined, tools)
                if _out:
                    self._sse(to_openai_chunk(
                        content=_out, model=mapped, cid=cid, created=created))
            if tool_calls:
                tc_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": mapped,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": i,
                                "id": self._tool_call_id(cid, i),
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                                },
                            } for i, tc in enumerate(tool_calls)]
                        },
                        "finish_reason": "tool_calls",
                    }],
                }
                self._sse(tc_chunk)
            else:
                # 压住的内容最终判定不是工具调用 —— 清理残留后补发给用户，
                # 否则这段文本就凭空消失了。
                rest = _strip_residue(guard.drain(), tools)
                if rest:
                    sent_content = True
                    self._sse(to_openai_chunk(
                        content=rest, model=mapped, cid=cid, created=created))
                self._sse(to_openai_chunk(finish_reason="stop", model=mapped,
                                          cid=cid, created=created))
                # 末尾补 usage（部分客户端靠它算耗时/计费）
                # ★ OpenAI 规范：usage 单独一个 chunk，choices 为**空数组**，
                #   且必须在 finish_reason 之后。deepseek-harness 等客户端
                #   依赖 stream_options.include_usage 统计 token。
                if usage:
                    self._sse({
                        "id": cid, "object": "chat.completion.chunk",
                        "created": created, "model": mapped,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": usage.get("promptTokens", 0),
                            "completion_tokens": usage.get("completionTokens", 0),
                            "total_tokens": (usage.get("promptTokens", 0)
                                             + usage.get("completionTokens", 0)),
                        },
                    })
            self._write(b"data: [DONE]\n\n")
            self._flush()
            # ★ chunks=0 不能算 ok：客户端会一片空白而日志毫无异常，
            #   必须标成警告，方便一眼看出"上游没给内容"。
            if seq == 0:
                log(f"⚠️ stream 空响应 model={mapped} "
                    f"reasoning={sent_reasoning} content={sent_content} "
                    f"——上游未返回任何内容，请查看上方错误")
            else:
                log(f"stream ok model={mapped} reasoning={sent_reasoning} "
                    f"content={sent_content} chunks={seq}")

        except _NET_ERRS:
            # 客户端断开：正常现象，静默结束
            self._client_gone = True
            return
        except Exception as e:
            if self._client_gone:
                return
            account.mark_fail(e)
            log(f"stream ERR: {e}")
            self._sse({"error": {"message": self._err_str(e)}})

    # ---------- 非流式 ----------
    def handle_non_stream(self, req):
        account = POOL.get_next()
        if not account or not account.is_valid():
            self._send_json(503, {"error": {"message": "no valid mimo account"}})
            return

        model = req.get("model", "mimo-v2.6-pro")
        messages = req.get("messages", [])
        tools = req.get("tools")
        reasoning_effort = req.get("reasoning_effort", "default")
        # 一次请求内固定，用于生成稳定的 tool_call id
        cid = f"chatcmpl-{int(time.time())}"

        multi_medias = self._upload_medias(account, messages, req)
        conv_id = self._conversation_id(req)
        payload, mapped = build_mimo_payload(
            messages, model, tools=tools,
            reasoning_effort=reasoning_effort,
            conversation_id=conv_id,
            history_mode=CFG.get("mimo", "history_mode", "full"),
            temperature=req.get("temperature"),
            top_p=req.get("top_p"),
            web_search=self._web_search(req),
            tool_choice=req.get("tool_choice"),
            parallel_tool_calls=req.get("parallel_tool_calls", True))
        CONVERSATIONS.put(self._conv_key(req), payload["conversationId"])
        payload["multiMedias"] = multi_medias

        try:
            raw = call_mimo(account, payload)
            events = parse_sse_events(raw)

            content_parts = []
            reasoning_parts = []
            usage = None
            for evt, data in events:
                if not isinstance(data, dict):
                    continue
                if data.get("messageType") == "assistant":
                    if "content" in data:
                        content_parts.append(data["content"])
                    if "reasoningContent" in data:
                        reasoning_parts.append(data["reasoningContent"])
                if "promptTokens" in data or "completionTokens" in data:
                    usage = data

            full = "".join(content_parts)
            full_reasoning = "".join(reasoning_parts)
            # stop 参数：上游不支持，在代理侧软截断
            full, _hit = self._apply_stop(full, req.get("stop"))
            if _hit is not None:
                full_reasoning = full_reasoning

            # 工具调用提取
            tool_calls = _extract_calls(full, tools)
            # ★ claw 内置工具由代理自己执行并回灌，客户端无需实现
            _claw = self._claw_auto_loop(account, req, tools, mapped,
                                         conv_id, tool_calls)
            if _claw:
                _c_text, _c_reason, _c_rounds = _claw
                if _c_reason:
                    full_reasoning = (full_reasoning or "") + _c_reason
                full = _c_text
                tool_calls = []
            if tool_calls:
                # 有工具调用, 清理正文
                clean = _strip_residue(full, tools)
                resp = {
                    "id": cid,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": mapped,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": clean,
                            "tool_calls": [{
                                "id": self._tool_call_id(cid, i),
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                                },
                            } for i, tc in enumerate(tool_calls)],
                        },
                        "finish_reason": "tool_calls",
                    }],
                }
            else:
                # ★ 空正文兜底：思考型模型有时把答案全写进 reasoning，
                #   content 为空 -> 客户端显示空白。用 reasoning 补位。
                _content = (full or "").strip()
                if not _content and (full_reasoning or "").strip() \
                        and self._fallback_on_empty():
                    _content = full_reasoning.strip()
                msg = {"role": "assistant", "content": _content}
                if full_reasoning:
                    msg["reasoning"] = full_reasoning
                resp = {
                    "id": cid,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": mapped,
                    "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": usage.get("promptTokens", 0) if usage else 0,
                        "completion_tokens": usage.get("completionTokens", 0) if usage else 0,
                        "total_tokens": (usage.get("promptTokens", 0) + usage.get("completionTokens", 0)) if usage else 0,
                    },
                }
            self._send_json(200, resp)

        except _NET_ERRS:
            # 客户端断开：不是账号的问题，不能计入失败
            self._client_gone = True
            return
        except Exception as e:
            if self._client_gone:
                return
            account.mark_fail(e)
            self._send_json(502, {"error": {"message": self._err_str(e)}})

    # ---------- 通用：取账号 + 上传 + 跑一轮 MiMo ----------
    def _run_mimo(self, messages, model, tools=None, reasoning_effort="default",
                  req=None):
        """返回 (account, mapped_model, full_text, reasoning, usage)"""
        account = POOL.get_next()
        if not account or not account.is_valid():
            raise RuntimeError("no valid mimo account")

        multi_medias = self._upload_medias(account, messages, req)
        # req 可为 None（内部辅助调用），此时只从 messages 里抽附件
        payload, mapped = build_mimo_payload(
            messages, model, tools=tools,
            reasoning_effort=reasoning_effort)
        payload["multiMedias"] = multi_medias

        raw = call_mimo(account, payload)
        events = parse_sse_events(raw)

        content_parts, reasoning_parts, usage = [], [], None
        for evt, data in events:
            if not isinstance(data, dict):
                continue
            if data.get("messageType") == "assistant":
                if "content" in data:
                    content_parts.append(data["content"])
                if "reasoningContent" in data:
                    reasoning_parts.append(data["reasoningContent"])
            if "promptTokens" in data or "completionTokens" in data:
                usage = data
        return account, mapped, "".join(content_parts), "".join(reasoning_parts), usage

    # ---------- Anthropic /v1/messages ----------
    def handle_anthropic(self, req):
        model_in = req.get("model", "claude-sonnet-4-5")
        model = resolve_anthropic_model(model_in)
        messages = anthropic_to_openai_messages(req)
        tools = anthropic_tools_to_openai(req.get("tools"))
        stream = req.get("stream", False)
        max_tokens = req.get("max_tokens", 4096)

        # reasoning_effort 可从 thinking 字段推断
        thinking = req.get("thinking")
        reasoning_effort = "default"
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            reasoning_effort = "high"
        elif thinking is None:
            reasoning_effort = "none"

        # 流式时绝不先跑 _run_mimo()：它会把上游读完才返回。
        if not stream:
            try:
                account, mapped, full, reasoning, usage = self._run_mimo(
                    messages, model, tools=tools,
                    reasoning_effort=reasoning_effort, req=req)
            except Exception as e:
                STATS["failed"] += 1
                log(f"Anthropic ERR: {e}")
                self._send_json(502, {
                    "type": "error",
                    "error": {"type": "api_error", "message": self._err_str(e)},
                })
                return
            tool_calls = _extract_calls(full, tools)
            log(f"Anthropic {model_in}->{mapped} ok, tools={len(tool_calls)}")
            self._send_json(200, build_anthropic_response(
                full, reasoning, tool_calls, model_in, usage))
            return

        # ---- 真流式：thinking -> text -> tool_use ----
        # 不能走 _run_mimo()，它会把上游读完才返回，客户端会在同一瞬间
        # 收到全部事件，深度思考时间显示为 0。
        account = POOL.get_next()
        if not account or not account.is_valid():
            self._send_json(503, {"type": "error",
                "error": {"type": "api_error", "message": "no valid mimo account"}})
            return

        try:
            multi_medias = self._upload_medias(account, messages, req)
            payload, mapped = build_mimo_payload(
                messages, model, tools=tools,
                reasoning_effort=reasoning_effort,
                conversation_id=self._conversation_id(req),
                history_mode=CFG.get("mimo", "history_mode", "full"),
                temperature=req.get("temperature"),
                top_p=req.get("top_p"),
                web_search=self._web_search(req),
            tool_choice=req.get("tool_choice"),
            parallel_tool_calls=req.get("parallel_tool_calls", True))
            CONVERSATIONS.put(self._conv_key(req),
                              payload["conversationId"])
            payload["multiMedias"] = multi_medias
        except Exception as e:
            STATS["failed"] += 1
            log(f"Anthropic ERR: {e}")
            self._send_json(502, {"type": "error",
                "error": {"type": "api_error", "message": self._err_str(e)}})
            return

        # ★ 强制非流式：先判，header 要在 end_headers 之前发
        oneshot = self._force_nonstream()

        # SSE 无 Content-Length，客户端靠连接关闭判断结束
        self.close_connection = True
        self.send_response(200)
        for h, v in [("Content-Type", "text/event-stream"),
                     ("Cache-Control", "no-cache"),
                     ("Connection", "close"),
                     ("Access-Control-Allow-Origin", "*")]:
            self.send_header(h, v)
        if oneshot:
            self.send_header("X-Mimo-Stream", "oneshot")
        self.end_headers()
        self._started = True

        def ev(t, **kw):
            # Anthropic 要求 data JSON 里带 type，客户端靠它分派解析器。
            # 不加 sequence_number（那是 Responses 的字段）。
            payload = dict(kw)
            payload["type"] = t
            return self._write(
                f"event: {t}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
            ) and self._flush()

        msg_id = f"msg_{gen_msg_id()[:16]}"
        ev("message_start", message={
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model_in,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

        try:
            idx = 0
            in_reasoning = False
            in_text = False
            parts = []
            reason_parts = []
            usage_s = None
            seq = 0
            guard = ToolTextGuard(enabled=bool(_eff_tools(tools)))

            for _e, data in call_mimo_stream(account, payload):
                if self._client_gone:
                    return
                if _e == "__heartbeat__":
                    if not self._write(b": ping\n\n"):
                        return
                    self._flush()
                    continue
                for kind, val in normalize_event(data):
                    if kind == "usage":
                        usage_s = val
                        continue
                    if kind == "error":
                        # ★ 不再 continue 吞掉：转成正文发给客户端。
                        #   否则上游繁忙/风控时客户端整屏空白且不知原因。
                        #   HAR 实证: "result":"服务器繁忙，请稍后再试"
                        etext = self._upstream_err_text(val)
                        log(f"上游错误事件: {etext[:150]}")
                        seq += 1
                        self._sse({"type": "content_block_delta", "index": idx,
                                   "delta": {"type": "text_delta",
                                             "text": etext}})
                        continue
                    if not val:
                        continue

                    if kind == "reasoning":
                        reason_parts.append(val)
                        if oneshot:
                            continue
                        if not in_reasoning:
                            ev("content_block_start", index=idx, content_block={
                                "type": "thinking", "thinking": "", "signature": ""})
                            in_reasoning = True
                        seq += 1
                        if not ev("content_block_delta", index=idx,
                                  delta={"type": "thinking_delta", "thinking": val}):
                            return
                    elif kind == "content":
                        parts.append(val)
                        if oneshot:
                            continue
                        if in_reasoning:
                            ev("content_block_stop", index=idx)
                            in_reasoning = False
                            idx += 1
                        if not in_text:
                            ev("content_block_start", index=idx,
                               content_block={"type": "text", "text": ""})
                            in_text = True
                        seq += 1
                        # ★ 疑似工具调用的片段先压住，别把 XML 念给用户
                        safe = guard.feed(val)
                        if safe and not ev("content_block_delta", index=idx,
                                          delta={"type": "text_delta",
                                                 "text": safe}):
                            return

            if in_reasoning:
                ev("content_block_stop", index=idx)
                idx += 1

            joined = "".join(parts)
            tool_calls = _extract_calls(joined, tools)

            if oneshot:
                # 攒齐后一次性发：thinking 一块 + text 一块，
                # 之后按原逻辑发 tool_use / message_delta / message_stop。
                rj = "".join(reason_parts).strip()
                if rj:
                    ev("content_block_start", index=idx, content_block={
                        "type": "thinking", "thinking": "", "signature": ""})
                    ev("content_block_delta", index=idx,
                       delta={"type": "thinking_delta", "thinking": rj})
                    ev("content_block_stop", index=idx)
                    idx += 1
                _out = _strip_residue(joined, tools)
                if _out:
                    ev("content_block_start", index=idx,
                       content_block={"type": "text", "text": ""})
                    ev("content_block_delta", index=idx,
                       delta={"type": "text_delta", "text": _out})
                    ev("content_block_stop", index=idx)
                    idx += 1
                in_reasoning = False
                in_text = False

            if in_text:
                # 压住的内容最终不是工具调用 —— 清理残留后补发，别让它消失
                rest = _strip_residue(guard.drain(), tools) if not tool_calls else ""
                if rest:
                    ev("content_block_delta", index=idx,
                       delta={"type": "text_delta", "text": rest})
                ev("content_block_stop", index=idx)
                idx += 1

            for tc in tool_calls:
                args = tc.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"raw": args}
                tid = f"toolu_{gen_msg_id()[:12]}"
                ev("content_block_start", index=idx, content_block={
                    "type": "tool_use", "id": tid,
                    "name": tc.get("name", ""), "input": {}})
                ev("content_block_delta", index=idx, delta={
                    "type": "input_json_delta",
                    "partial_json": json.dumps(args, ensure_ascii=False)})
                ev("content_block_stop", index=idx)
                idx += 1

            stop = "tool_use" if tool_calls else "end_turn"
            ev("message_delta", delta={"stop_reason": stop, "stop_sequence": None},
               usage={"output_tokens": usage_s.get("completionTokens", 0) if usage_s else 0})
            ev("message_stop")
            log(f"Anthropic stream {model_in}->{mapped} ok chunks={seq} "
                f"tools={len(tool_calls)}")

        except _NET_ERRS:
            self._client_gone = True
            return
        except Exception as e:
            if self._client_gone:
                return
            account.mark_fail(e)
            log(f"Anthropic stream ERR: {e}")
            ev("error", type="api_error", error={"type": "api_error",
                                                 "message": self._err_str(e)})

    # ---------- OpenAI Responses API ----------
    def handle_responses(self, req):
        model = MODEL_MAP.get(req.get("model", "mimo-v2.6-pro"), req.get("model", "mimo-v2.6-pro"))
        messages = responses_input_to_messages(req.get("input", ""))
        # instructions 等价于 system
        instructions = req.get("instructions", "")
        if instructions:
            messages.insert(0, {"role": "system", "content": instructions})
        tools = responses_tools_to_openai(req.get("tools"))
        stream = req.get("stream", False)
        reasoning_effort = (req.get("reasoning") or {}).get("effort", "default") \
            if isinstance(req.get("reasoning"), dict) else "default"

        resp_id = f"resp_{gen_msg_id()[:16]}"
        cid = resp_id          # 供 _tool_call_id 生成稳定的 call_id
        base = {
            "id": resp_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output": [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": req.get("tools") or [],
        }

        # 流式时绝不先跑 _run_mimo()：它会把上游读完才返回
        if not stream:
            try:
                account, mapped, full, reasoning, usage = self._run_mimo(
                    messages, model, tools=tools,
                    reasoning_effort=reasoning_effort, req=req)
            except Exception as e:
                STATS["failed"] += 1
                log(f"Responses ERR: {e}")
                self._send_json(502, {"error": {"message": self._err_str(e)}})
                return
            tool_calls = _extract_calls(full, tools)
            log(f"Responses {mapped} ok, tools={len(tool_calls)}")
            base["model"] = mapped
            base["output"] = build_responses_output(reasoning, full, tool_calls)
            if usage:
                base["usage"] = {
                    "input_tokens": usage.get("promptTokens", 0),
                    "output_tokens": usage.get("completionTokens", 0),
                    "total_tokens": usage.get("promptTokens", 0) + usage.get("completionTokens", 0),
                }
            base["output_text"] = _strip_residue(full, tools) if tool_calls else full
            self._send_json(200, base)
            return

        # ---- 真流式 ----
        # 和 Anthropic 同理：不能先 _run_mimo() 读完再发，否则客户端
        # 一次性收到全部事件，思考耗时显示为 0。
        account = POOL.get_next()
        if not account or not account.is_valid():
            self._send_json(503, {"error": {"message": "no valid mimo account"}})
            return

        try:
            multi_medias = self._upload_medias(account, messages, req)
            payload, mapped = build_mimo_payload(
                messages, model, tools=tools,
                reasoning_effort=reasoning_effort,
                conversation_id=self._conversation_id(req),
                history_mode=CFG.get("mimo", "history_mode", "full"),
                temperature=req.get("temperature"),
                top_p=req.get("top_p"),
                web_search=self._web_search(req),
            tool_choice=req.get("tool_choice"),
            parallel_tool_calls=req.get("parallel_tool_calls", True))
            CONVERSATIONS.put(self._conv_key(req),
                              payload["conversationId"])
            payload["multiMedias"] = multi_medias
        except Exception as e:
            STATS["failed"] += 1
            log(f"Responses ERR: {e}")
            self._send_json(502, {"error": {"message": self._err_str(e)}})
            return

        self.close_connection = True
        # ★ 强制非流式：先判，header 要在 end_headers 之前发
        oneshot = self._force_nonstream()

        self.send_response(200)
        for h, v in [("Content-Type", "text/event-stream"),
                     ("Cache-Control", "no-cache"),
                     ("Connection", "close"),
                     ("Access-Control-Allow-Origin", "*")]:
            self.send_header(h, v)
        if oneshot:
            self.send_header("X-Mimo-Stream", "oneshot")
        self.end_headers()
        self._started = True

        def ev(t, **kw):
            # OpenAI Responses 约定：事件类型同时出现在 SSE `event:` 行
            # **和** data JSON 的 `type` 字段里。客户端（openai-python 等）
            # 是从 data JSON 读 type 来分派解析器的；只给 event: 行而
            # data 里没有 type，就会报 "chunk type not found"。
            payload = resp_event(t, **kw)
            return self._write(
                f"event: {t}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
            ) and self._flush()

        rs_id = f"rs_{gen_msg_id()[:12]}"
        msg_item = f"msg_{gen_msg_id()[:12]}"

        # created / in_progress 阶段状态必须是 in_progress，
        # 否则部分客户端认为响应已结束，不再接收后续 delta
        base["status"] = "in_progress"
        ev("response.created", response=base)
        ev("response.in_progress", response=base)

        try:
            seq = 0
            parts = []
            reason_parts = []
            usage_s = None
            opened_rs = False
            guard = ToolTextGuard(enabled=bool(_eff_tools(tools)))
            opened_msg = False

            for _e, data in call_mimo_stream(account, payload):
                if self._client_gone:
                    return
                if _e == "__heartbeat__":
                    if not self._write(b": ping\n\n"):
                        return
                    self._flush()
                    continue
                for kind, val in normalize_event(data):
                    if kind == "usage":
                        usage_s = val
                        continue
                    if kind == "error":
                        # ★ 不再 continue 吞掉：转成正文发给客户端。
                        #   否则上游繁忙/风控时客户端整屏空白且不知原因。
                        #   HAR 实证: "result":"服务器繁忙，请稍后再试"
                        etext = self._upstream_err_text(val)
                        log(f"上游错误事件: {etext[:150]}")
                        seq += 1
                        self._sse({"type": "response.output_text.delta",
                                   "output_index": 0, "item_id": msg_item,
                                   "sequence_number": seq, "delta": etext})
                        continue
                    if not val:
                        continue

                    if kind == "reasoning":
                        reason_parts.append(val)
                        if oneshot:
                            continue
                        if not opened_rs:
                            ev("response.output_item.added", output_index=0,
                               sequence_number=seq,
                               item={"type": "reasoning", "id": rs_id,
                                     "summary": []})
                            opened_rs = True
                        seq += 1
                        if not ev("response.reasoning_text.delta", item_id=rs_id,
                                  output_index=0, sequence_number=seq,
                                  delta=val):
                            return
                    elif kind == "content":
                        parts.append(val)
                        if oneshot:
                            continue
                        if opened_rs:
                            ev("response.output_item.done", output_index=0,
                               sequence_number=seq,
                               item={"type": "reasoning", "id": rs_id,
                                     "summary": []})
                            opened_rs = False
                        if not opened_msg:
                            seq += 1
                            ev("response.output_item.added", output_index=1,
                               sequence_number=seq,
                               item={"type": "message", "id": msg_item,
                                     "role": "assistant",
                                     "status": "in_progress", "content": []})
                            seq += 1
                            ev("response.content_part.added", item_id=msg_item,
                               output_index=1, sequence_number=seq,
                               part={"type": "output_text", "text": "",
                                     "annotations": []})
                            opened_msg = True
                        seq += 1
                        # ★ 疑似工具调用的片段先压住，别把 XML 念给用户
                        safe = guard.feed(val)
                        if safe and not ev("response.output_text.delta",
                                          item_id=msg_item, output_index=1,
                                          sequence_number=seq, delta=safe):
                            return

            joined = "".join(parts)
            tool_calls = _extract_calls(joined, tools)
            text = _strip_residue(joined, tools) if tool_calls else joined
            # 压住的内容最终不是工具调用 —— 补发 delta，别让它消失
            if not tool_calls:
                rest = guard.drain()
                if rest and opened_msg:
                    seq += 1
                    ev("response.output_text.delta", item_id=msg_item,
                       output_index=1, sequence_number=seq,
                       delta=_strip_residue(rest, tools))

            if oneshot:
                # 攒齐后一次性发：reasoning 一项 + message 一项，
                # 之后按原逻辑发 function_call / completed。
                rj = "".join(reason_parts).strip()
                if rj:
                    seq += 1
                    ev("response.output_item.added", output_index=0,
                       sequence_number=seq,
                       item={"type": "reasoning", "id": rs_id, "summary": []})
                    seq += 1
                    ev("response.reasoning_text.delta", item_id=rs_id,
                       output_index=0, sequence_number=seq, delta=rj)
                    seq += 1
                    ev("response.output_item.done", output_index=0,
                       sequence_number=seq,
                       item={"type": "reasoning", "id": rs_id, "summary": []})
                _out = _strip_residue(joined, tools)
                if _out:
                    seq += 1
                    ev("response.output_item.added", output_index=1,
                       sequence_number=seq,
                       item={"type": "message", "id": msg_item,
                             "role": "assistant", "status": "in_progress",
                             "content": []})
                    seq += 1
                    ev("response.content_part.added", item_id=msg_item,
                       output_index=1, sequence_number=seq,
                       part={"type": "output_text", "text": "",
                             "annotations": []})
                    seq += 1
                    ev("response.output_text.delta", item_id=msg_item,
                       output_index=1, sequence_number=seq, delta=_out)
                    # 收尾三连，否则部分客户端认为文本块没结束
                    seq += 1
                    ev("response.output_text.done", item_id=msg_item,
                       output_index=1, sequence_number=seq, text=_out)
                    seq += 1
                    ev("response.content_part.done", item_id=msg_item,
                       output_index=1, sequence_number=seq,
                       part={"type": "output_text", "text": _out,
                             "annotations": []})
                    seq += 1
                    ev("response.output_item.done", item_id=msg_item,
                       output_index=1, sequence_number=seq,
                       item={"type": "message", "id": msg_item,
                             "role": "assistant", "status": "completed",
                             "content": [{"type": "output_text", "text": _out,
                                          "annotations": []}]})
                opened_rs = False
                opened_msg = False

            if opened_msg:
                seq += 1
                ev("response.output_text.done", item_id=msg_item, output_index=1,
                   sequence_number=seq, text=text)
                seq += 1
                ev("response.content_part.done", item_id=msg_item, output_index=1,
                   sequence_number=seq,
                   part={"type": "output_text", "text": text, "annotations": []})
                seq += 1
                ev("response.output_item.done", item_id=msg_item, output_index=1,
                   sequence_number=seq,
                   item={"type": "message", "id": msg_item, "role": "assistant",
                         "status": "completed",
                         "content": [{"type": "output_text", "text": text,
                                      "annotations": []}]})

            for i, tc in enumerate(tool_calls):
                fid = f"fc_{gen_msg_id()[:12]}"
                args = tc.get("arguments", {})
                args_s = args if isinstance(args, str) else \
                    json.dumps(args, ensure_ascii=False)
                seq += 1
                ev("response.output_item.added", output_index=2 + i,
                   sequence_number=seq,
                   item={"type": "function_call", "id": fid,
                         "call_id": self._tool_call_id(cid, i),
                         "name": tc.get("name", ""), "arguments": "",
                         "status": "in_progress"})
                seq += 1
                ev("response.function_call_arguments.delta", item_id=fid,
                   output_index=2 + i, sequence_number=seq, delta=args_s)
                seq += 1
                ev("response.output_item.done", output_index=2 + i,
                   sequence_number=seq,
                   item={"type": "function_call", "id": fid,
                         "call_id": self._tool_call_id(cid, i),
                         "name": tc.get("name", ""), "arguments": args_s,
                         "status": "completed"})

            if usage_s:
                base["usage"] = {
                    "input_tokens": usage_s.get("promptTokens", 0),
                    "output_tokens": usage_s.get("completionTokens", 0),
                    "total_tokens": (usage_s.get("promptTokens", 0)
                                     + usage_s.get("completionTokens", 0)),
                }
            base["output"] = build_responses_output("", text, tool_calls)
            base["output_text"] = text
            base["status"] = "completed"
            seq += 1
            ev("response.completed", response=base)
            log(f"Responses stream {mapped} ok chunks={seq} tools={len(tool_calls)}")

        except _NET_ERRS:
            self._client_gone = True
            return
        except Exception as e:
            if self._client_gone:
                return
            account.mark_fail(e)
            log(f"Responses stream ERR: {e}")
            ev("error", message=self._err_str(e))

    # ---------- 上传辅助 ----------
    # ---------- 会话复用（多轮上下文） ----------
    def _conv_key(self, req):
        """确定会话键。优先级: 请求头 > user 字段 > 无(不复用)"""
        hdr = None
        try:
            hdr = self.headers.get("X-Mimo-Conversation-Id")
        except Exception:
            hdr = None
        if hdr:
            return str(hdr)[:200]
        u = req.get("user")
        if u:
            return "u:" + str(u)[:200]
        return None

    def _web_search(self, req):
        """是否开启联网搜索。默认关（与网页端一致）。

        判定顺序（命中即用）:
          1) 请求体 web_search / enable_web_search 布尔值
          2) 请求头 X-Mimo-Web-Search: enabled|disabled
          3) config.json mimo.web_search（字符串或布尔）
        """
        for key in ("web_search", "enable_web_search", "webSearch"):
            if key in req:
                v = req[key]
                if isinstance(v, str):
                    return v.lower() in ("1", "true", "on", "enabled", "yes")
                return bool(v)
        try:
            h = self.headers.get("X-Mimo-Web-Search")
        except Exception:
            h = None
        if h:
            return str(h).lower() in ("1", "true", "on", "enabled", "yes")
        d = CFG.get("mimo", "web_search", False)
        if isinstance(d, str):
            return d.lower() in ("1", "true", "on", "enabled", "yes")
        return bool(d)

    def _conversation_id(self, req):
        """取可复用的 conversationId；没有会话键则 None（每次新建）"""
        key = self._conv_key(req)
        if not key:
            return None
        return CONVERSATIONS.get(key)

    # ---------- 配额 / 会话（抓包新增接口） ----------
    @staticmethod
    def _collect_user_info():
        out = []
        for a in POOL.accounts:
            try:
                info = get_user_info(a)
            except Exception as e:
                info = {"error": str(e)[:120]}
            info["uid"] = getattr(a, "uid", None)
            info["valid"] = bool(getattr(a, "is_valid", lambda: False)())
            out.append(info)
        return out

    def _collect_quota(self):
        out = []
        for a in POOL.all():
            try:
                q = get_account_quota(a)
            except Exception as e:      # noqa: BLE001
                q = {"uid": getattr(a, "uid", None), "error": str(e)}
            out.append(q)
        return out

    def _collect_claw(self, wait=False):
        """汇总每个账号的 mimo-claw 容器状态

        wait=True 时若状态是 CREATING 会轮询到 AVAILABLE/FAILED。
        """
        out = []
        for a in POOL.accounts:
            try:
                st = claw_container(a, wait_available=wait)
            except Exception as e:      # noqa: BLE001
                st = {"error": str(e)[:200]}
            if isinstance(st, dict):
                st["uid"] = a.uid
            out.append(st)
        return out

    def _collect_conversations(self):
        out = []
        for a in POOL.all():
            try:
                r = list_conversations(a, conv_type="chat")
                out.append({"uid": getattr(a, "uid", None),
                            "total": r["total"], "list": r["list"][:20]})
            except Exception as e:      # noqa: BLE001
                out.append({"uid": getattr(a, "uid", None), "error": str(e)})
        return out

    @staticmethod
    def _explicit_files(req):
        """取请求里显式给出的文件，yield (文件名, 二进制)

        支持 files / file 字段（base64 或 data URI），
        以及 multipart 上传的 _files。
        """
        # ★ 必须返回列表：调用点直接 for ... in 结果，
        #   返回 None 会抛 "'NoneType' object is not iterable"
        if not req:
            return []
        out = []
        # multipart 上传
        for k in ("file", "files", "files[]"):
            for v in (req.get("_files") or {}).get(k, []) or []:
                if isinstance(v, (bytes, bytearray)):
                    out.append((f"upload-{len(out)}", bytes(v)))
        # JSON 里的 files 数组
        fl = req.get("files")
        if isinstance(fl, dict):
            fl = [fl]
        if isinstance(fl, list):
            for it in fl:
                if isinstance(it, dict):
                    nm = it.get("name") or it.get("filename") or f"file-{len(out)}"
                    d = it.get("data") or it.get("content") or it.get("b64") or ""
                else:
                    nm, d = f"file-{len(out)}", it
                try:
                    if "," in d and d.strip().startswith("data:"):
                        d = d.split(",", 1)[1]
                    raw = base64.b64decode(d)
                    if raw:
                        out.append((nm, raw))
                except Exception:
                    pass
        return out

    def _upload_medias(self, account, messages, req=None):
        """抽取并上传图片/文件, 返回 multiMedias 数组

        三种来源都支持：
          1) OpenAI content 数组里的 image_url / file part
          2) 请求体 files 字段: [{"name":"a.txt","data":"<base64>"}]
          3) multipart/form-data 里的 file 字段
        """
        query_text, imgs_b64, imgs_url, text_files = extract_medias(messages)

        medias = []
        # 来源 2/3 优先（用户显式指定的意图更明确）
        for nm, raw in self._explicit_files(req):
            try:
                mime = file_mime(nm, raw)
                mt = "image" if mime.startswith("image/") else "file"
                medias.append(upload_media(account, raw, mime,
                                           media_type=mt, filename=nm))
            except Exception as e:
                log(f"上传文件失败 {nm}: {str(e)[:120]}")
        for im in imgs_b64:
            try:
                obj = upload_media(account, im["b64"], im["mime"], media_type="image")
                medias.append(obj)
            except Exception as e:
                sys.stderr.write(f"[upload] image b64 failed: {e}\n")
        for url in imgs_url:
            try:
                r = requests.get(url, timeout=30)
                if r.status_code == 200:
                    b64 = base64.b64encode(r.content).decode()
                    mime = r.headers.get("content-type", "image/jpeg")
                    obj = upload_media(account, b64, mime, media_type="image")
                    medias.append(obj)
            except Exception as e:
                sys.stderr.write(f"[upload] image url {url} failed: {e}\n")
        for tf in text_files:
            try:
                obj = upload_media(account, tf["b64"], tf["mime"],
                                  media_type="file", filename=tf["filename"])
                medias.append(obj)
            except Exception as e:
                sys.stderr.write(f"[upload] file {tf['filename']} failed: {e}\n")
        return medias


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """多线程 + 地址复用 + 连接类异常静默

    1) 多线程：单线程时一个慢的流式请求会阻塞所有其他请求
    2) allow_reuse_address：重启不必等 TIME_WAIT
    3) handle_error：客户端断开属于正常现象，不打 traceback 刷屏
    """
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, _NET_ERRS):
            return                      # 客户端断开，静默
        if isinstance(exc, OSError) and getattr(exc, "errno", None) in (32, 104, 110):
            return
        super().handle_error(request, client_address)


def _auto_claw():
    """启动时为每个无容器的账号申请 mimo-claw 容器

    失败只打印不中断启动 —— 容器不可用不应导致代理起不来。
    """
    timeout = int(CFG.get("claw", "create_wait_timeout", 90) or 90)
    for a in POOL.accounts:
        try:
            st = ensure_container(a, auto_create=True, wait=True,
                                  timeout=timeout)
            status = st.get("status") or "无"
            extra = st.get("message") or ""
            if st.get("error"):
                print(f"   - {a.uid}: ❌ {st['error'][:70]}")
            else:
                print(f"   - {a.uid}: {status} {extra}".rstrip())
        except Exception as e:      # noqa: BLE001
            print(f"   - {a.uid}: ❌ {str(e)[:70]}")


def main():
    print("""
╔══════════════════════════════════════════════╗
║        MiMo v2.6 轻量代理（模块化）          ║
║   OpenAI → /open-apis/react/chat             ║
║   多账号轮询 · 工具调用 · 上传 · 自动刷新     ║
║   Anthropic · Responses · TTS · ASR · 面板    ║
╚══════════════════════════════════════════════╝
    """)
    try:
        import mimo_proxy.utils as _u
        if _u.DEBUG:
            print(f"🐛 debug 模式: 开（完整请求/响应已记日志"
                  f"{'，文件: ' + _u.DEBUG_FILE if _u.DEBUG_FILE else ''}）")
            print("   ⚠ 敏感信息会自动打码，但日志量较大，排查完记得关掉")
    except Exception:
        pass
    print(f"📡 监听: {CFG.host}:{CFG.port}")
    print(f"🔑 API Key: {CFG.api_key}")
    print(f"👥 账号数: {len(POOL.accounts)}")
    for a in POOL.accounts:
        print(f"   - {a}")
    print(f"🔄 自动刷新: {'开' if CFG.auto_refresh else '关'}")

    # mimo-claw：定时任务（一个号一个建 / 每号每天限次 / 每 N 小时一轮）
    global CLAW_SCHED
    if CFG.get("claw", "auto_schedule"):
        CLAW_SCHED = ClawScheduler(
            POOL, CFG,
            state_path=CFG.get("claw", "state_file") or "claw_state.json")
        CLAW_SCHED.start(run_now=bool(CFG.get("claw", "schedule_on_start")))
        _h = CFG.get("claw", "schedule_interval_hours", 3)
        _n = CFG.get("claw", "daily_limit_per_account", 1)
        print(f"🐾 claw 调度: 每 {_h} 小时一轮，每号每天最多 {_n} 次")

    # mimo-claw：启动时为无容器的账号自动申请（配置项 auto_create_on_start）
    if CFG.get("claw", "auto_create_on_start"):
        print("🐾 mimo-claw: 启动时自动申请容器")
        _auto_claw()
    print()
    print("📌 可用端点:")
    print("   POST /v1/chat/completions    (OpenAI)")
    print("   POST /v1/messages            (Anthropic)")
    print("   POST /v1/responses           (OpenAI Responses)")
    print("   GET  /v1/models")
    print(f"   GET  /admin                  (管理面板, 独立管理员账号登录)")
    print(f"   POST /api/admin/accounts    (网页添加账号: 账密 或 Cookie)")
    print(f"   💾 账号文件: {accounts_file()}")
    _users = ", ".join(str(a.get("user", "?")) for a in admin_accounts()[:3])
    print(f"        管理员: {_users}")
    print(f"       ⚙  配置文件: {CFG.get('_loaded_from') or '（默认值）'}")
    print("\n✅ 启动成功\n")

    server = _QuietThreadingHTTPServer((CFG.host, CFG.port), Handler)

    # 可选 HTTPS：config.json 里配 server.tls.cert / server.tls.key 即启用。
    # 客户端若坚持用 https://，光靠 HTTP 端口是连不上的（会看到 TLS 乱码 + 400）。
    # 注意：CFG.get("server", "tls") 返回的是整个 tls 字典。
    # 之前写成 CFG.get("server","tls","cert") 拿到的仍是这个 dict，
    # 再传给 os.path.exists() 会抛
    # "TypeError: stat: path should be string... not dict"。
    # 这里先取字典、再用 .get() 取字段，并做类型保护。
    _tls = CFG.get("server", "tls")
    tls_cert = tls_key = None
    if isinstance(_tls, dict):
        tls_cert = _tls.get("cert")
        tls_key = _tls.get("key")
    # 配置写错（比如写成数字/字典）时不要崩，提示后回退 HTTP
    if tls_cert is not None and not isinstance(tls_cert, str):
        print(f"⚠️  server.tls.cert 不是字符串（{type(tls_cert).__name__}），已忽略")
        tls_cert = None
    if tls_key is not None and not isinstance(tls_key, str):
        print(f"⚠️  server.tls.key 不是字符串（{type(tls_key).__name__}），已忽略")
        tls_key = None
    if tls_cert and tls_key:
        import ssl
        if not (os.path.exists(tls_cert) and os.path.exists(tls_key)):
            print(f"⚠️  TLS 证书文件不存在: cert={tls_cert} key={tls_key}")
            print("    已回退为 HTTP")
        else:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                ctx.load_cert_chain(tls_cert, tls_key)
            except Exception as e:
                print(f"⚠️  加载证书失败: {e}，已回退为 HTTP")
            else:
                server.socket = ctx.wrap_socket(server.socket, server_side=True)
                print(f"🔒 已启用 HTTPS  (base url: https://{CFG.host}:{CFG.port})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 停止")
        server.server_close()