# -*- coding: utf-8 -*-
"""
工具调用：OpenAI tools <-> MiMoML 提示词注入 / 提取 / 多轮序列化

设计要点
============================================================
上游 MiMo 是**纯文本单轮**接口，没有原生 function calling。
所以工具调用靠"提示词注入 + 文本提取"模拟，这决定了三件事必须做扎实：

1) **多轮工具交互必须能回传**
   客户端执行完工具后，把结果以 role="tool" 发回来。
   旧实现完全忽略这类消息 —— 模型看不到工具结果，多轮调用直接断链。

2) **提取必须抗噪**
   模型输出的调用块经常：未闭合、参数非 JSON、工具名写错大小写、
   外面裹 markdown 围栏、重复输出。旧实现只处理"完美格式"。

3) **误判必须杜绝**
   见上一轮修复：必须有工具名白名单，否则正文里的 JSON 会被当成调用。
"""

import json
import re
import difflib


# ---------------------------------------------------------------- 基础

def tool_names(tools):
    """从 OpenAI tools 里取出合法工具名集合"""
    names = set()
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", {}) or {}
        n = fn.get("name") or t.get("name")
        if n:
            names.add(str(n).strip())
    return names


def tool_schemas(tools):
    """{工具名: parameters}"""
    out = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", {}) or {}
        n = fn.get("name") or t.get("name")
        if n:
            out[str(n).strip()] = fn.get("parameters") or {}
    return out


def _norm_name(n):
    """工具名归一化：小写 + 去掉 _ - . 空格"""
    return re.sub(r"[_\-.\s]", "", str(n or "")).lower()


def match_tool_name(name, valid_names, cutoff=0.82):
    """把模型写出的工具名映射到白名单里的真实名字

    依次尝试：精确 -> 归一化 -> difflib 模糊 -> 互为子串
    失败返回 None（宁可不算调用，也不要造出不存在的工具名）。
    """
    if not name or not valid_names:
        return None
    s = str(name).strip()
    if s in valid_names:
        return s

    # 大小写不敏感
    low = {str(x).strip(): x for x in valid_names}
    if s.lower() in {k.lower() for k in low}:
        for k, v in low.items():
            if k.lower() == s.lower():
                return v

    # 归一化（去掉 _ - . 空格）
    ns = _norm_name(s)
    for x in valid_names:
        if _norm_name(x) == ns:
            return x

    # 模糊匹配
    cand = difflib.get_close_matches(s, list(valid_names), n=1, cutoff=cutoff)
    if cand:
        return cand[0]

    # 互为子串（模型常加前缀/后缀，如 get_weather_tool）
    for x in valid_names:
        a, b = _norm_name(x), ns
        if a and b and (a in b or b in a) and min(len(a), len(b)) >= 4:
            return x
    return None


# ---------------------------------------------------------------- 参数修复

_FENCE = re.compile(r"^\s*```(?:json|tool|tool_call|javascript)?\s*|\s*```\s*$",
                    re.M)


def _fix_json(raw):
    """尽力把参数文本解析成 dict

    容错：markdown 围栏 / 尾随逗号 / 单引号 / 非 JSON 的 k=v / k:v /
          外面套了多余文本时取第一个 {...}
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    s = str(raw).strip()
    if not s:
        return {}

    # 已经能解析
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        pass

    # 去 markdown 围栏
    s2 = _FENCE.sub("", s).strip()
    if s2 and s2 != s:
        try:
            v = json.loads(s2)
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            s = s2

    # 取第一个 {...} 或 [...]
    m = re.search(r"\{.*\}|\[.*\]", s, re.S)
    if m:
        cand = m.group(0)
        for fix in (cand, re.sub(r",\s*([}\]])", r"\1", cand),
                    cand.replace("'", '"')):
            try:
                v = json.loads(fix)
                return v if isinstance(v, dict) else {"value": v}
            except Exception:
                pass

    # k=v / k:v 形式（& 或 换行 或 逗号 分隔）
    out = {}
    kv = re.findall(r"([\w\u4e00-\u9fff\-]+)\s*[:=]\s*([^,;\n]+)", s)
    if kv:
        for k, v in kv:
            v = v.strip().strip('"').strip("'")
            try:
                v = json.loads(v)
            except Exception:
                pass
            out[k] = v
        if out:
            return out

    # 实在不行：原样包一层
    return {"raw": s}


def _validate_args(name, args, schemas):
    """按 schema 补必填、剔除未知字段（宽松版）

    只做"能救回来"的事：
      - 缺必填且 schema 有 default -> 补 default
      - 参数名大小写/归一化不匹配 -> 纠正到 schema 里的名字
    缺必填但无 default 时**不报错**（上游是文本模型，强行拒绝只会更糟）。
    """
    schema = (schemas or {}).get(name)
    if not isinstance(schema, dict):
        return args
    props = schema.get("properties")
    if not isinstance(props, dict) or not isinstance(args, dict):
        return args

    # 参数名纠正
    fixed = {}
    keys = list(props.keys())
    for k, v in args.items():
        if k in props:
            fixed[k] = v
            continue
        nk = _norm_name(k)
        hit = None
        for p in keys:
            if _norm_name(p) == nk:
                hit = p
                break
        if hit is None:
            hit = match_tool_name(k, set(keys), cutoff=0.7)
        fixed[hit or k] = v

    # 补 default
    for p, spec in props.items():
        if p not in fixed and isinstance(spec, dict) and "default" in spec:
            fixed[p] = spec["default"]
    return fixed


# ---------------------------------------------------------------- 提取

# 完整标签
RE_FULL = re.compile(r'<TOOL_CALL\s+name="([^"]+)"\s*>(.*?)</TOOL_CALL>', re.S)
# 未闭合标签（流式截断 / 模型漏写结束标签）—— 吃到结尾或下一个开始标签
RE_OPEN = re.compile(
    r'<TOOL_CALL\s+name="([^"]+)"\s*>(.*?)(?=<TOOL_CALL\s|$)', re.S)
RE_KV = re.compile(r'TOOL_CALL\s*[:：]\s*(\S+)\s*(.*)', re.S)
RE_FENCE = re.compile(r'```(?:tool_call|tool)\s*\n(.*?)\n?```', re.S)
RE_JSON = re.compile(
    r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}', re.S)

# ---------------------------------------------------------------------------
# 策略 6/7：其它框架的工具调用格式
#
# 模型不一定按我们注入的格式输出。它见过哪些格式取决于训练数据，
# 而客户端(AstrBot 等)的系统提示词也可能描述另一套格式，
# 两套提示并存时模型会挑它更熟悉的那个写。
# 认不出格式的后果很严重：整段 <function=...> 会原样当正文发给用户，
# 界面上就是"AI 把工具调用代码念出来了"。
# ---------------------------------------------------------------------------

# AstrBot / llama.cpp 风格:
#   <function=name><parameter=p1>v1</parameter></function>
RE_FUNC = re.compile(
    r'<function\s*[=:]\s*([^\s>]+)\s*>(.*?)</function\s*>', re.S | re.I)
# 兼容 <function>name</function> 这种把名字放 body 里的写法
RE_FUNC_BODY = re.compile(r'<function\s*>(.*?)</function\s*>', re.S | re.I)
RE_PARAM = re.compile(
    r'<parameter\s*[=:]\s*([^\s>]+)\s*>(.*?)</parameter\s*>', re.S | re.I)
# 小写变体: <tool_call>{"name":..,"arguments":{..}}</tool_call>
RE_LOWER = re.compile(r'<tool_call\s*>(.*?)</tool_call\s*>', re.S | re.I)


def _xml_params_to_json(body):
    """把 <parameter=k>v</parameter> 序列转成 dict

    值可能是纯文本，也可能是 JSON（嵌套对象/数组）。
    先试 JSON，失败就当字符串 —— 别擅自推断类型。
    """
    if not body:
        return None
    pairs = RE_PARAM.findall(body)
    if not pairs:
        return None
    out = {}
    for k, v in pairs:
        v = (v or "").strip()
        try:
            out[k] = json.loads(v)
        except Exception:
            out[k] = v
    return out


def extract_tool_calls(text, valid_names=None, schemas=None,
                       allow_fuzzy=True, dedupe=True, max_calls=8):
    """提取工具调用（多策略 + 白名单 + 容错）

    valid_names : 客户端提供的工具名集合（强烈建议传）
    schemas     : {工具名: parameters}，用于参数名纠正与补全
    allow_fuzzy : 工具名是否允许模糊匹配（大小写/近似）
    dedupe      : 相同 (name, arguments) 是否去重
    max_calls   : 单次最多返回几个调用
    """
    if not text:
        return []

    def resolve(n):
        if not valid_names:
            return n
        return match_tool_name(n, valid_names) if allow_fuzzy else (
            n if n in valid_names else None)

    raw = []

    # 策略 1: 完整标签
    for m in RE_FULL.finditer(text):
        raw.append((m.group(1), m.group(2)))
    # 策略 2: 未闭合标签（仅在没找到完整标签时启用）
    if not raw:
        for m in RE_OPEN.finditer(text):
            body = m.group(2).strip()
            if body:
                raw.append((m.group(1), body))
    # 策略 3: TOOL_CALL: xxx
    if not raw:
        for m in RE_KV.finditer(text):
            raw.append((m.group(1), m.group(2)))
    # 策略 4: ```tool_call 代码块
    if not raw:
        for m in RE_FENCE.finditer(text):
            try:
                o = json.loads(m.group(1))
                raw.append((o.get("name", ""), o.get("arguments", {})))
            except Exception:
                pass
    # 策略 5: 裸 JSON {"name":..,"arguments":..}
    if not raw:
        for m in RE_JSON.finditer(text):
            raw.append((m.group(1), m.group(2)))

    # 策略 6: AstrBot / llama 风格 <function=name><parameter=..>..</function>
    if not raw:
        for m in RE_FUNC.finditer(text):
            params = _xml_params_to_json(m.group(2))
            raw.append((m.group(1),
                        json.dumps(params, ensure_ascii=False)
                        if params is not None else m.group(2)))
    # 策略 7: 小写 <tool_call>{json}</tool_call>
    if not raw:
        for m in RE_LOWER.finditer(text):
            body = (m.group(1) or "").strip()
            try:
                o = json.loads(body)
                raw.append((o.get("name", ""), o.get("arguments", {})))
            except Exception:
                # body 里可能是 <function=..> 的嵌套写法
                sub = RE_FUNC.search(body)
                if sub:
                    params = _xml_params_to_json(sub.group(2))
                    raw.append((sub.group(1),
                                json.dumps(params, ensure_ascii=False)
                                if params is not None else sub.group(2)))

    calls, seen = [], set()
    for name, argtext in raw:
        if len(calls) >= max_calls:
            break
        real = resolve(name)
        if not real:
            continue
        args = _fix_json(argtext)
        if schemas:
            args = _validate_args(real, args, schemas)
        if dedupe:
            key = (real, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if key in seen:
                continue
            seen.add(key)
        calls.append({"name": real, "arguments": args})
    return calls


# ---------------------------------------------------------------- 注入

def _slim_params(params, max_chars=600, max_props=12):
    """精简 JSON Schema：砍长描述、限制属性数，避免提示词爆炸"""
    if not isinstance(params, dict):
        return params
    out = {"type": params.get("type", "object")}
    props = params.get("properties")
    if isinstance(props, dict):
        kept = {}
        for i, (k, v) in enumerate(props.items()):
            if i >= max_props:
                break
            if isinstance(v, dict):
                vv = {kk: vv for kk, vv in v.items() if kk != "description"}
                desc = v.get("description")
                if desc:
                    vv["description"] = str(desc)[:120]
                kept[k] = vv
            else:
                kept[k] = v
        out["properties"] = kept
    req = params.get("required")
    if isinstance(req, list):
        out["required"] = req[:max_props]
    if len(json.dumps(out, ensure_ascii=False)) > max_chars:
        keys = list(out.get("properties", {}).keys())
        out = {"type": "object", "properties": {k: "" for k in keys},
               "required": out.get("required", [])}
    return out


def inject_tools_prompt(system_prompt, tools, max_tools=32,
                        max_desc=160, tool_choice=None,
                        parallel_tool_calls=True):
    """把 OpenAI tools 转成 MiMoML 提示词注入到 system

    ★ 不输出"活"的 <TOOL_CALL> 完整示例 —— 否则模型复述时会被策略1命中，
      造成误判。改成占位写法（标签名与尖括号之间留空格）。
    """
    if not tools:
        return system_prompt or ""
    if tool_choice == "none":
        return system_prompt or ""

    items = [t for t in tools if isinstance(t, dict)]
    truncated = 0
    if max_tools and len(items) > max_tools:
        truncated = len(items) - max_tools
        items = items[:max_tools]

    lines = ["[TOOLS]", "你可以使用以下工具。需要调用时，输出且仅输出一个调用块，"
                        "不要在同一条回复里混其他内容。", ""]
    for t in items:
        fn = t.get("function", {}) or {}
        name = fn.get("name") or t.get("name") or ""
        desc = str(fn.get("description") or "")[:max_desc]
        params = fn.get("parameters")
        lines.append(f"- {name}: {desc}")
        if params:
            lines.append(f"  参数: {json.dumps(_slim_params(params), ensure_ascii=False)}")
    if truncated:
        lines.append(f"- (另有 {truncated} 个工具未列出，已超出上限 {max_tools})")
    lines.append("")
    lines.append("调用写法：< TOOL_CALL > 开始标签中带 name 属性，"
                 "标签体内是参数 JSON，最后用 </ TOOL_CALL > 结束。")
    if parallel_tool_calls:
        lines.append("可以一次输出多个调用块来并行调用多个工具。")
    else:
        # deepseek-harness 等客户端会发 parallel_tool_calls:false，
        # 表示一次只允许一个工具调用。上游没有原生开关，只能提示。
        lines.append("一次只能调用**一个**工具，不要并行调用多个。")
    lines.append("不需要调用工具时，按正常方式回答即可，不要输出任何调用块。")

    if isinstance(tool_choice, dict):
        fn = (tool_choice.get("function") or {}).get("name")
        if fn:
            lines.append(f"本次必须调用工具: {fn}")
    lines.append("[END_TOOLS]")
    return (system_prompt or "") + "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------- 多轮序列化

def _tool_call_text(tc):
    """OpenAI tool_call -> 可读文本"""
    fn = tc.get("function", {}) or {}
    name = fn.get("name", "")
    args = fn.get("arguments", "")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except Exception:
            pass
    if isinstance(args, dict):
        args = json.dumps(args, ensure_ascii=False)
    return f'{name}({args})'


def _content_text(content, pending=None):
    """content 可能是 str / list[blocks]"""
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text":
                parts.append(p.get("text", ""))
            elif t == "tool_result":
                # Anthropic 的 tool_result（放在 user 消息的 content 里）
                out = p.get("content")
                if isinstance(out, list):
                    out = " ".join(
                        x.get("text", "") for x in out if isinstance(x, dict))
                # 优先显示工具名，其次才是 id
                tid = p.get("tool_use_id") or ""
                label = (pending or {}).get(tid) or tid
                parts.append(f'[工具结果] {label}: {out}')
            elif t == "tool_use":
                parts.append(f'[工具调用] {p.get("name","")}'
                             f'({json.dumps(p.get("input",{}), ensure_ascii=False)})')
        return "\n".join(x for x in parts if x)
    return content or ""


def serialize_tool_turns(messages):
    """把含工具调用的 OpenAI messages 序列化成 [(role, text)]

    ★ 这是"健全工具调用"的核心：让上游模型能看到完整的多轮工具交互。

    处理三类特殊消息:
      - assistant 带 tool_calls -> "[工具调用] name({args})"
      - role="tool"             -> "[工具结果] id: 内容"
      - Anthropic user 消息里的 tool_result block -> 同上
    """
    turns = []
    pending = {}          # tool_call_id / tool_use_id -> 工具名
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        # ★ developer 是 OpenAI 新风格的系统角色（deepseek-harness、
        #   o 系列客户端在用）。不归一的话整个消息会被丢弃 ——
        #   客户端发来的系统指令、工具说明全部失效。
        if role == "developer":
            role = "system"
        text = _content_text(m.get("content", ""), pending)

        # ★ Anthropic 的 assistant 消息把工具调用放在 content 的
        #   tool_use block 里（而不是 tool_calls 字段），
        #   这里要先把 id->name 记下来，否则后面的 tool_result
        #   只能显示成一个没有名字的裸结果。
        if role == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    if b.get("id") and b.get("name"):
                        pending[b["id"]] = b["name"]

        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                cid = tc.get("id") or ""
                name = (tc.get("function", {}) or {}).get("name", "")
                if cid and name:
                    pending[cid] = name
                turns.append(("assistant", f"[工具调用] {_tool_call_text(tc)}"))
            if text:
                turns.append(("assistant", text))
            continue

        if role == "tool":
            cid = m.get("tool_call_id") or ""
            name = pending.get(cid, "")
            label = f"[工具结果] {name}" if name else "[工具结果]"
            turns.append(("user", f"{label}: {text}"))
            continue

        if role in ("user", "assistant", "system"):
            turns.append((role, text))
    return turns


# ---------------------------------------------------------------- 清理

def strip_tool_residue(text):
    """清理响应中的工具调用残留标签/JSON, 只留正文

    无条件清理噪声（即使最终没有有效工具调用），
    否则模型复述的 <TOOL_CALL ...> 会原样显示给用户。
    """
    if not text:
        return ""
    text = RE_FULL.sub('', text)
    # 未闭合标签也要清（模型漏写结束标签时会残留半截）
    text = re.sub(r'<TOOL_CALL\s+name="[^"]+"\s*>.*?(?=<TOOL_CALL\s|$)', '',
                  text, flags=re.S)
    text = re.sub(r'</?TOOL_CALL[^>]*>', '', text)
    text = re.sub(r'TOOL_CALL\s*[:：]\s*\S+.*', '', text)
    text = RE_FENCE.sub('', text)
    # 其它框架的格式也要清，否则会原样显示给用户
    # （AstrBot 的 <function=..><parameter=..> 就属于这一类）
    text = RE_FUNC.sub('', text)
    text = RE_FUNC_BODY.sub('', text)
    text = RE_LOWER.sub('', text)
    # 未闭合的外层开标签：模型常写成 <tool_call><function=..>..</function>
    # 而漏掉 </tool_call>，清完内层后会剩一个孤零零的 <tool_call>。
    text = re.sub(r'<tool_call\s*>', '', text, flags=re.I)
    # 孤立残留（模型只写了半截）
    text = re.sub(r'</?(?:function|parameter|tool_call)\s*[=:][^>]*>',
                  '', text, flags=re.I)
    text = re.sub(r'</(?:function|parameter|tool_call)\s*>', '', text,
                  flags=re.I)
    text = re.sub(r'\[TOOLS\].*?\[END_TOOLS\]', '', text, flags=re.S)
    text = re.sub(r'<!\\[CDATA\\[.*?\\]\\]>', '', text, flags=re.S)
    return text.strip()


# ---------------------------------------------------------------------------
# 流式抑制：不要把工具调用的 XML 念给用户听
#
# 流式是逐字转发的。工具调用也是逐字到达的 ——
# 如果边收边发，用户会先看到一整段
#     <function=astrbot_execute_shell><parameter=command>...
# 然后客户端才收到真正的 tool_calls。
# 界面上的表现就是"AI 把工具调用代码念出来了"。
#
# 办法：碰到疑似工具调用的开头就先压住不发，
# 等流结束后由 extract_tool_calls 统一处理；
# 若最终判定不是工具调用，再把压住的内容放出来。
# ---------------------------------------------------------------------------

RE_TOOL_HINT = re.compile(
    r'<(?:/\s*)?(?:TOOL_CALL|tool_call|function|parameter)\b'
    r'|```\s*(?:tool_call|tool)\b'
    r'|\bTOOL_CALL\s*[:：]',
    re.I)


class StopFilter:
    """流式下按 OpenAI stop 参数截断

    上游不支持 stop，但 deepseek-harness 等客户端会传。
    流式是边收边发的，事后再截断没用 —— 必须在发送前过滤：
      - 缓冲区里出现完整 stop 序列 -> 截断、标记结束
      - 缓冲区尾部是某个 stop 序列的**前缀** -> 先压住不发
        （否则 "<|STO" 已经发给用户，等 "P|>" 到达就收不回来了）
    """

    def __init__(self, stop):
        if not stop:
            self.seqs = []
        elif isinstance(stop, str):
            self.seqs = [stop] if stop else []
        else:
            self.seqs = [x for x in stop if isinstance(x, str) and x]
        self.buf = ""
        self.done = False

    def active(self):
        return bool(self.seqs)

    def feed(self, chunk):
        if not self.seqs:
            return chunk
        if self.done:
            return ""
        self.buf += chunk or ""
        # 1) 完整匹配 -> 截断
        best = None
        for sq in self.seqs:
            i = self.buf.find(sq)
            if i >= 0 and (best is None or i < best):
                best = i
        if best is not None:
            out = self.buf[:best]
            self.buf = ""
            self.done = True
            return out
        # 2) 尾部是前缀 -> 压住
        maxlen = max(len(x) for x in self.seqs)
        keep = 0
        for k in range(min(maxlen - 1, len(self.buf)), 0, -1):
            suf = self.buf[-k:]
            if any(x.startswith(suf) for x in self.seqs):
                keep = k
                break
        if keep:
            out = self.buf[:len(self.buf) - keep]
            self.buf = self.buf[len(self.buf) - keep:]
            return out
        out, self.buf = self.buf, ""
        return out

    def drain(self):
        """流结束时取出压住的部分（没构成完整 stop，应正常发出）"""
        if self.done:
            return ""
        out, self.buf = self.buf, ""
        return out


class ToolTextGuard:
    """流式下缓冲"疑似工具调用"的片段

    feed(chunk) -> 可以立刻发给客户端的文本（可能为 ""）
    drain()     -> 流结束时取出仍未发出的部分
    """

    # 一个未写完的标签最多可能占多少字符。
    # 流式是逐字到达的，"<funct" 这种半截标签不能提前发出去 ——
    # 否则等 "ion=..." 到达时开头已经发给用户了，再也收不回来。
    TAIL_KEEP = 64

    def __init__(self, max_hold=4000, enabled=True):
        self.buf = ""
        self.max_hold = max_hold
        # enabled=False：客户端自带原生工具协议时不介入，文本原样透传
        self.enabled = enabled

    def feed(self, chunk):
        if not self.enabled:
            return chunk or ""
        if not chunk:
            return ""
        self.buf += chunk
        m = RE_TOOL_HINT.search(self.buf)
        if m:
            if len(self.buf) - m.start() > self.max_hold:
                # 压了太久还没闭合，基本可以确定不是工具调用，放出来
                out, self.buf = self.buf, ""
                return out
            out = self.buf[:m.start()]
            self.buf = self.buf[m.start():]
            return out
        # 没匹配到完整标签：保留最后一个 "<" 或反引号之后的尾巴，
        # 防止半截标签被提前发出。没有这两个字符就不必谨慎。
        last = max(self.buf.rfind("<"), self.buf.rfind("`"))
        if last < 0 or len(self.buf) - last > self.TAIL_KEEP:
            out, self.buf = self.buf, ""
            return out
        out = self.buf[:last]
        self.buf = self.buf[last:]
        return out

    def drain(self):
        if not self.enabled:
            return ""
        out, self.buf = self.buf, ""
        return out

    def empty(self):
        return not self.buf


# ---------------------------------------------------------------- claw 内置工具

# mimo-claw 沙箱能力包装成 OpenAI 工具，让 agent（dsh / AstrBot / rikkahub）
# 能直接使用沙箱，而不必自己去实现一套 claw 客户端。
#
# 开启方式：config.json 的 claw.expose_tools = true
# 代理会在客户端传入的 tools 后面追加这些定义；模型调用它们时，
# 若 claw.auto_execute = true，代理会自己执行并把结果回灌给上游，
# 客户端无需实现任何东西。

CLAW_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "claw_container_status",
            "description": "查询 mimo-claw 沙箱容器状态与剩余配额（token / 存储）",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claw_list_files",
            "description": "列出 mimo-claw 沙箱中的文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string",
                                        "description": "目录路径，留空为根目录"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claw_read_file",
            "description": "读取 mimo-claw 沙箱中的文件内容",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string",
                                        "description": "文件路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claw_list_sessions",
            "description": "列出 mimo-claw 沙箱会话",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claw_read_session",
            "description": "读取某个 mimo-claw 沙箱会话的历史消息",
            "parameters": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "string",
                                       "description": "会话 ID"}},
                "required": ["conversationId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claw_ws_ticket",
            "description": "获取 mimo-claw 沙箱的 WebSocket 连接票据",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

CLAW_TOOL_NAMES = [t["function"]["name"] for t in CLAW_TOOLS]


def claw_tools_enabled():
    """是否把 claw 沙箱能力注入给模型"""
    try:
        import mimo_proxy.config as _c
        return bool(_c.CFG.get("claw", "expose_tools"))
    except Exception:
        return False


def append_claw_tools(tools):
    """在客户端工具后面追加 claw 内置工具（去重）"""
    if not claw_tools_enabled():
        return tools
    have = set()
    for t in tools or []:
        if isinstance(t, dict):
            n = (t.get("function") or {}).get("name") or t.get("name")
            if n:
                have.add(n)
    out = list(tools or [])
    for t in CLAW_TOOLS:
        if t["function"]["name"] not in have:
            out.append(t)
    return out


def claw_execute(account, name, args):
    """代理侧执行 claw 内置工具，返回给模型的文本结果

    失败也返回可读文本（不抛异常），否则模型会陷入重试循环。
    文本截断到 4000 字符，避免撑爆上游上下文。
    """
    import json as _j
    try:
        import mimo_proxy.mimo as _m
    except Exception as e:      # noqa: BLE001
        return f"[claw 错误] 无法加载上游模块: {e}"

    args = args if isinstance(args, dict) else {}
    try:
        if name == "claw_container_status":
            r = _m.claw_container(account)
        elif name == "claw_list_files":
            r = _m.claw_files(account, "list", args.get("path", ""))
        elif name == "claw_read_file":
            r = _m.claw_files(account, "preview", args.get("path", ""))
        elif name == "claw_list_sessions":
            r = _m.claw_conversations(account)
        elif name == "claw_read_session":
            r = _m.claw_dialog(account, args.get("conversationId", ""))
        elif name == "claw_ws_ticket":
            r = _m.ws_ticket(account)
        else:
            return f"[claw 错误] 未知工具: {name}"
    except Exception as e:      # noqa: BLE001
        return f"[claw 错误] {name} 执行失败: {str(e)[:150]}"

    if isinstance(r, dict) and r.get("error"):
        hint = r.get("hint") or ""
        return f"[claw 错误] {name}: {r['error']} {hint}".strip()
    txt = _j.dumps(r, ensure_ascii=False)
    if len(txt) > 4000:
        txt = txt[:4000] + "...（已截断）"
    return txt


# ------------------------------------------------------------------ 工具目录
# GET  /v1/tools       列出代理可直接执行的工具
# POST /v1/tools/call  直接执行（客户端不必实现这些工具）

def build_tool_catalog():
    """可用工具清单

    只有 claw 内置工具能被代理直接执行；客户端自带的 tools 是每次
    请求临时传入的，代理无从得知，只能在使用时校验。
    """
    import mimo_proxy.config as _c
    enabled = claw_tools_enabled()
    items = []
    for t in CLAW_TOOLS:
        f = t.get("function") or {}
        items.append({
            "name": f.get("name"),
            "description": f.get("description"),
            "parameters": f.get("parameters") or {},
            "executable": enabled,
        })
    return {
        "object": "list",
        "enabled": enabled,
        "count": len(items),
        "data": items,
        "note": ("claw.expose_tools=true 时这些工具会注入给模型；"
                 "claw.auto_execute=true 时代理自己执行并回灌结果"),
    }


def execute_tool(name, args, uid=None):
    """代理侧执行一个工具，返回 {"ok","name","result"/"error"}

    目前只支持 claw 内置工具。客户端自带的工具代理执行不了 ——
    那需要客户端自己实现并回传结果（走标准 tool 消息即可）。
    """
    if name not in CLAW_TOOL_NAMES:
        return {"ok": False, "name": name,
                "error": f"未知或不可直接执行的工具: {name}。"
                         f"可调: {', '.join(CLAW_TOOL_NAMES)}",
                "available": CLAW_TOOL_NAMES}
    if not claw_tools_enabled():
        return {"ok": False, "name": name,
                "error": "claw.expose_tools 未开启，拒绝执行"}

    try:
        import mimo_proxy.accounts as _a
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "name": name, "error": f"加载账号池失败: {e}"}

    pool = getattr(_a, "POOL", None)
    accounts = list(getattr(pool, "accounts", []) or [])
    if uid:
        accounts = [a for a in accounts if str(getattr(a, "uid", "")) == str(uid)]
    accounts = [a for a in accounts if getattr(a, "valid", True)]
    if not accounts:
        return {"ok": False, "name": name, "error": "没有可用账号"}

    last = None
    for a in accounts[:3]:      # 最多试 3 个号，避免逐个打上游
        try:
            out = claw_execute(a, name, args)
        except Exception as e:      # noqa: BLE001
            last = str(e)[:200]
            continue
        # claw_execute 失败会返回 "[claw 错误] ..." 文本
        if isinstance(out, str) and out.startswith("[claw 错误]"):
            last = out
            continue
        return {"ok": True, "name": name, "result": out,
                "uid": str(getattr(a, "uid", ""))}
    return {"ok": False, "name": name, "error": last or "执行失败",
            "tried": min(len(accounts), 3)}
