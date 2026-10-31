# -*- coding: utf-8 -*-
"""
协议转换层：Anthropic /v1/messages、OpenAI /v1/responses、OpenAI chunk
"""

import json
import time
import uuid

from .config import CFG
from .utils import log, gen_msg_id
from .mimo import MODEL_MAP
from .tools import strip_tool_residue

ANTHROPIC_MODEL_MAP = {
    "claude-opus-4": "mimo-v2.6-pro",
    "claude-opus-4-5": "mimo-v2.6-pro",
    "claude-sonnet-4": "mimo-v2.6-pro",
    "claude-sonnet-4-5": "mimo-v2.6-pro",
    "claude-3-7-sonnet": "mimo-v2.6-pro",
    "claude-3-5-sonnet": "mimo-v2.6-pro",
    "claude-haiku-4": "mimo-v2.6-flash",
    "claude-haiku-4-5": "mimo-v2.6-flash",
    "claude-3-5-haiku": "mimo-v2.6-flash",
}


def resolve_anthropic_model(model):
    """Claude 名 -> MiMo 名; 已是 mimo-* 则原样返回"""
    if not model:
        return "mimo-v2.6-pro"
    if model.startswith("mimo-"):
        return MODEL_MAP.get(model, model)
    return ANTHROPIC_MODEL_MAP.get(model, "mimo-v2.6-pro")


def anthropic_to_openai_messages(body):
    """Anthropic messages -> OpenAI messages。
    system 可为 str 或 [{type:text,text:...}] 数组。
    content 可为 str 或 block 数组(text/image/tool_result/tool_use)。
    """
    out = []
    sys_prompt = body.get("system", "")
    if isinstance(sys_prompt, list):
        parts = []
        for b in sys_prompt:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        sys_prompt = "\n".join(parts)
    if sys_prompt:
        out.append({"role": "system", "content": sys_prompt})

    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = m.get("content")
        blocks = []

        def add_block(c):
            if isinstance(c, str):
                blocks.append({"type": "text", "text": c})
            elif isinstance(c, list):
                for it in c:
                    add_block(it)
            elif isinstance(c, dict):
                t = c.get("type")
                if t == "text":
                    blocks.append({"type": "text", "text": c.get("text", "")})
                elif t == "image":
                    src = c.get("source", {})
                    if src.get("type") == "base64":
                        mime = src.get("media_type", "image/png")
                        blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{src.get('data','')}"},
                        })
                    elif src.get("type") == "url":
                        blocks.append({"type": "image_url", "image_url": {"url": src.get("url", "")}})
                elif t == "tool_result":
                    # 工具结果 -> 文本塞回去
                    rc = c.get("content", "")
                    if isinstance(rc, list):
                        rc = " ".join(x.get("text", "") for x in rc if isinstance(x, dict))
                    blocks.append({"type": "text",
                                   "text": f"[tool_result id={c.get('tool_use_id','')}] {rc}"})
                elif t == "tool_use":
                    blocks.append({"type": "text",
                                   "text": f"[tool_use] {c.get('name')}({json.dumps(c.get('input',{}), ensure_ascii=False)})"})

        add_block(content)

        # Anthropic 允许 assistant 消息; OpenAI 侧统一映射
        oai_role = "assistant" if role == "assistant" else "user"
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            out.append({"role": oai_role, "content": blocks[0]["text"]})
        else:
            out.append({"role": oai_role, "content": blocks})
    return out


def anthropic_tools_to_openai(tools):
    """Anthropic tools(name/description/input_schema) -> OpenAI tools(name/parameters)"""
    if not tools:
        return None
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


def build_anthropic_response(full_text, reasoning, tool_calls, model, usage):
    """组装 Anthropic messages 响应: content blocks 顺序 thinking -> text -> tool_use"""
    blocks = []
    if reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if tool_calls:
        clean = strip_tool_residue(full_text)
        if clean:
            blocks.append({"type": "text", "text": clean})
        for tc in tool_calls:
            args = tc.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"raw": args}
            blocks.append({
                "type": "tool_use",
                "id": f"toolu_{gen_msg_id()[:12]}",
                "name": tc.get("name", ""),
                "input": args,
            })
        stop_reason = "tool_use"
    else:
        blocks.append({"type": "text", "text": full_text})
        stop_reason = "end_turn"

    prompt_tok = usage.get("promptTokens", 0) if usage else 0
    compl_tok = usage.get("completionTokens", 0) if usage else 0
    return {
        "id": f"msg_{gen_msg_id()[:16]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": prompt_tok, "output_tokens": compl_tok},
    }


def responses_tools_to_openai(tools):
    """Responses API 的 tools 无 function 嵌套层, name 在顶层; 兼容两种写法"""
    if not tools:
        return None
    out = []
    for t in tools:
        if "function" in t:
            out.append(t)
        else:
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            })
    return out


def responses_input_to_messages(inp):
    """Responses 的 input 既可以是 str, 也可以是 [{role,content}] 数组"""
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    msgs = []
    for it in inp:
        if not isinstance(it, dict):
            continue
        role = it.get("role", "user")
        content = it.get("content", "")
        # function_call / function_call_output 特殊块转文本
        if it.get("type") == "function_call":
            msgs.append({"role": "assistant",
                         "content": f"[tool_use] {it.get('name')}({it.get('arguments','')})"})
        elif it.get("type") == "function_call_output":
            msgs.append({"role": "user",
                         "content": f"[tool_result] {it.get('output','')}"})
        else:
            msgs.append({"role": role, "content": content})
    if not msgs:
        msgs = [{"role": "user", "content": ""}]
    return msgs


def build_responses_output(reasoning, full_text, tool_calls):
    """Responses output 顺序: reasoning -> function_call -> message"""
    out = []
    if reasoning:
        out.append({
            "type": "reasoning",
            "id": f"rs_{gen_msg_id()[:12]}",
            "summary": [],
            "content": [{"type": "input_text", "text": reasoning}],
        })
    for tc in tool_calls:
        out.append({
            "type": "function_call",
            "id": f"fc_{gen_msg_id()[:12]}",
            "call_id": f"call_{gen_msg_id()[:8]}",
            "name": tc.get("name", ""),
            "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False)
            if not isinstance(tc.get("arguments"), str) else tc.get("arguments"),
            "status": "completed",
        })
    text = strip_tool_residue(full_text) if tool_calls else full_text
    if text:
        out.append({
            "type": "message",
            "id": f"msg_{gen_msg_id()[:12]}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    return out


def resp_event(event_type, **kw):
    """Responses SSE 事件包装"""
    ev = {"type": event_type, "sequence_number": kw.pop("seq", 0)}
    ev.update(kw)
    return ev


def to_openai_chunk(content=None, reasoning=None, finish_reason=None,
                    model="mimo-v2.6-pro", cid=None, created=None):
    """构造 OpenAI SSE chunk。

    思考内容同时写 reasoning_content 和 reasoning：
    前者是 DeepSeek / 多数 OpenAI 兼容客户端（LobeChat、Cherry Studio、
    RikkaHub 等）读取的字段名，后者供少数读 reasoning 的客户端使用。

    cid / created 在一次请求内必须固定：若每次都用 time.time() 现算，
    跨秒时 id 会变化，部分客户端会认为是不同条消息。
    """
    now = created if created is not None else int(time.time())
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
        delta["reasoning"] = reasoning
    return {
        "id": cid or f"chatcmpl-{now}",
        "object": "chat.completion.chunk",
        "created": now,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }