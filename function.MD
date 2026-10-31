# Function Calling（工具调用）完整说明

> 适用于 MiMo 代理 `mimo_proxy`
> 最后更新：2026-10-30 · 对应版本：模块化 v2（含 MCP 修复 + 健全化）

---

## 0. 一句话原理

**小米 MiMo 上游是纯文本单轮接口，没有原生 function calling。**

代理用「提示词注入 + 文本提取」模拟 OpenAI 的 function calling：

```
客户端发 tools[]      →  代理转成提示词注入 system
模型输出调用块文本     →  代理正则提取 → 转成 tool_calls 返回
客户端执行工具        →  结果以 role="tool" 回传
代理序列化成文本      →  上游模型看到工具结果 → 继续回答
```

这决定了三件事必须做扎实：**多轮能回传、提取抗噪、误判要杜绝**。下面逐条说明。

---

## 1. 支持的端点

| 端点 | 协议 | 工具调用支持 |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI Chat | ✅ 流式 / 非流式 |
| `POST /v1/messages` | Anthropic Messages | ✅ 流式 / 非流式 |
| `POST /v1/responses` | OpenAI Responses | ✅ 流式 / 非流式 |

三个端点共用同一套提取与注入逻辑，行为一致。

---

## 2. 请求格式

### 2.1 OpenAI 风格

```json
{
  "model": "mimo-v2.6-pro",
  "messages": [
    {"role": "user", "content": "北京天气怎么样？"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询指定城市天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名"}
          },
          "required": ["city"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

`tool_choice` 取值：

| 值 | 行为 |
|---|---|
| `"auto"` / 不传 | 正常注入工具 |
| `"none"` | **不注入**工具提示词，模型按普通方式回答 |
| `{"function": {"name": "x"}}` | 注入并追加「本次必须调用工具: x」 |

### 2.2 Anthropic 风格

工具在 `tools[].name` / `input_schema`（不是 `function.name`）：

```json
{
  "tools": [
    {"name": "get_weather", "description": "...", "input_schema": {...}}
  ]
}
```

两种写法都认，内部统一处理。

### 2.3 Responses 风格

`tools[]` 同 OpenAI。输出走 `response.output_item.added` +
`response.function_call_arguments.delta` + `response.output_item.done` 事件序列。

---

## 3. 响应格式

模型决定调用工具时，`finish_reason` 为 `tool_calls`：

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "",
      "tool_calls": [{
        "id": "call_chatcmpl-abc123_0",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"city\": \"北京\"}"
        }
      }],
      "finish_reason": "tool_calls"
    }
  }]
}
```

**`id` 是确定性的**：`call_{响应id}_{序号}`。请求内唯一、跨请求可区分，
客户端回传 `tool_call_id` 时代理能反查工具名。（早期版本用随机 id，无法反查。）

---

### developer 角色

OpenAI o1 之后引入 `developer`，语义等同 `system`。
deepseek-harness 等客户端会用它下系统指令。

代理在两处归一成 `system`：
- `mimo.py::_normalize_roles()` —— 主路径
- `tools.py::serialize_tool_turns()` —— 兜底

不归一的话这条消息会被整体丢弃，客户端发的系统指令、
工具说明全部失效。

## 4. 完整多轮流程

```
① 用户: "北京天气？"
   客户端带 tools[] 发出
   ↓
② 代理: 注入 [TOOLS] 提示词 + 历史 → 上游
   ↓
③ 模型: 输出 <TOOL_CALL name="get_weather">{"city":"北京"}</TOOL_CALL>
   ↓
④ 代理: 提取 → tool_calls，finish_reason="tool_calls"，正文清理残留
   ↓
⑤ 客户端: 执行工具，拿到结果
   ↓
⑥ 客户端: 把结果发回来
   {
     "messages": [
       {"role":"user", "content":"北京天气？"},
       {"role":"assistant", "tool_calls":[{...id=call_xxx_0...}]},
       {"role":"tool", "tool_call_id":"call_xxx_0", "content":"晴 25度"}
     ]
   }
   ↓
⑦ 代理: serialize_tool_turns() 把整段历史转成文本
   "[工具调用] get_weather({\"city\":\"北京\"})"
   "[工具结果] get_weather: 晴 25度"
   ↓
⑧ 上游模型看到结果 → 生成最终回答
```

**第 ⑦ 步是早期版本的断点**：`role="tool"` 消息被整个丢弃，模型永远看不到工具结果，
多轮直接断链。现已修复，OpenAI 与 Anthropic 两种风格都覆盖。

---

## 5. 注入的提示词长什么样

```
[TOOLS]
你可以使用以下工具。需要调用时，输出且仅输出一个调用块，不要在同一条回复里混其他内容。

- get_weather: 查询指定城市天气
  参数: {"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}

调用写法：< TOOL_CALL > 开始标签中带 name 属性，标签体内是参数 JSON，最后用 </ TOOL_CALL > 结束。
可以一次输出多个调用块来并行调用多个工具。
不需要调用工具时，按正常方式回答即可，不要输出任何调用块。
[END_TOOLS]
```

### 为什么标签写成 `< TOOL_CALL >` 而不是 `<TOOL_CALL>`

**这是踩过坑的。** 早期提示词里给了「活」的完整示例：

```xml
<TOOL_CALL name="工具名">{"a":1}</TOOL_CALL>
```

模型在回答里**复述**这个格式时，被提取策略 1 原样命中 → 误判成工具调用
→ 客户端去执行不存在的工具 → **rikkahub 前端整屏空白**（就是那个"开 MCP 不回复"的 bug）。

现在标签名与尖括号之间留空格，模型看得懂但不被正则匹配。

### 提示词瘦身

40 个 MCP 工具全量注入时提示词达 22981 字符（≈7.6K tokens），叠加历史容易顶到上游上限。

| 措施 | 效果 |
|---|---|
| `max_tools` 上限（默认 32） | 超出部分提示「另有 N 个工具未列出」 |
| schema 精简 | 砍长描述（限 120 字符）、限属性数（12）、整体超 600 字符退化为纯 key 列表 |
| 结果 | 22981 → **9100 字符** |

---

## 6. 提取：7 种策略 + 白名单

### 6.1 七种识别方式（按优先级，命中即止）

| # | 格式 | 例子 |
|---|---|---|
| 1 | 完整标签 | `<TOOL_CALL name="x">{...}</TOOL_CALL>` |
| 2 | **未闭合标签** | `<TOOL_CALL name="x">{"a":1}`（流式截断 / 模型漏写） |
| 3 | KV 写法 | `TOOL_CALL: x {"a":1}` |
| 4 | 代码块 | ```` ```tool_call\n{...}\n``` ```` |
| 5 | 裸 JSON | `{"name":"x","arguments":{...}}` |
| 6 | **AstrBot / llama 风格** | `<function=x><parameter=k>v</parameter></function>` |
| 7 | 小写标签 | `<tool_call>{"name":"x","arguments":{...}}</tool_call>` |

策略 2~7 仅在前序策略无命中时启用，避免重复提取。

#### 为什么要认策略 6/7

模型不一定按我们注入的格式输出 —— 训练数据里见过别的格式，
客户端（AstrBot 等）的系统提示词也可能描述另一套，
两套提示并存时模型会挑它更熟悉的那个写。

认不出的后果很严重：整段 `<function=...>` 会**原样当正文发给用户**，
界面上就是"AI 把工具调用代码念出来了"。

`<parameter=k>` 的值先按 JSON 解析，失败则当字符串 —— 不擅自推断类型。

真实 case（AstrBot 的 `astrbot_execute_shell`）：

```
我来试着装一下 ffmpeg～
<function=astrbot_execute_shell><parameter=command>which ffmpeg ...</parameter>
</function>
```

提取结果：`{"name":"astrbot_execute_shell","arguments":{"command":"which ffmpeg ..."}}`，
正文保留"我来试着装一下 ffmpeg～"。

### 6.2 工具名白名单（关键防误判）

**只有客户端实际提供的工具名才算调用。** 匹配顺序：

```
精确 → 大小写不敏感 → 归一化(去 _-. 空格) → difflib 模糊(0.82) → 互为子串
```

匹配不上返回 `None`，**不算调用**（宁可漏，也不造出不存在的工具名让客户端执行失败）。

这一条修掉了三类误判：

| 模型实际在说 | 修复前被当成 | 修复后 |
|---|---|---|
| 回答里夹了个 JSON `{"name":"my-server",...}` | 调用 `my-server` | ✅ 不误判 |
| 复述工具用法 `<TOOL_CALL name="工具名">` | 调用 `工具名` | ✅ 不误判 |
| 思考里提「`TOOL_CALL: xxx` 这种写法不对」 | 调用 `xxx` | ✅ 不误判 |

### 6.3 容错表

| 情况 | 例子 | 结果 |
|---|---|---|
| 未闭合标签 | `<TOOL_CALL name="x">{"a":1}` | ✅ |
| 参数非 JSON | `city=北京` | ✅ 修成 dict |
| markdown 围栏 | 裹在 ` ```json ` 里 | ✅ |
| 尾随逗号 | `{"a":1,}` | ✅ |
| 单引号 | `{'a':1}` | ✅ |
| 工具名大小写错 | `Get_Weather` | ✅ 映射 |
| 工具名加后缀 | `get_weather_tool` | ✅ 映射 |
| 参数名错 | `{"City":...}` | ✅ 纠正到 `city` |
| 重复调用 | 同一调用出现两次 | ✅ 去重 |
| 不存在的工具 | `{"name":"nope"}` | ✅ 返回空 |

参数实在解析不了时包成 `{"raw": "..."}` 而不是丢弃。

### 6.4 限制

- 单次最多 `max_calls=8` 个调用
- 支持**并行调用**：一次输出多个块，index 正确递增

---

### 6.5 流式抑制（ToolTextGuard）

流式是逐字转发的，工具调用也是逐字到达的。如果边收边发，
用户会先看到一整段 `<function=...><parameter=...>`，
然后客户端才收到真正的 `tool_calls`。

`ToolTextGuard` 的做法：碰到疑似工具调用的开头就**先压住不发**，
等流结束后由 `extract_tool_calls` 统一处理：

- 判定是工具调用 → 发 `tool_calls`，压住的文本丢弃（本来就是标签）
- 判定不是 → `strip_tool_residue()` 清理后补发给用户，文本不会凭空消失

三个端点（OpenAI / Anthropic / Responses）的流式都接了。

**分片切断问题**：流式按 6 字符一片，`<funct` 这种半截标签
不能提前发出 —— 等 `ion=...` 到达时开头已经发给用户，再也收不回来。
所以没匹配到完整标签时，保留最后一个 `<` 或反引号之后的尾巴
（上限 64 字符）。文本里没有这两个字符就直接全发，不引入延迟。

压住超过 4000 字符仍未闭合 → 判定不是工具调用，全部放出。

---

## 7. 残留清理

模型有时会输出半截标签或复述格式，`strip_tool_residue()` 无条件清理：

| 清理对象 | 说明 |
|---|---|
| 完整 `<TOOL_CALL>...</TOOL_CALL>` | 主调用块 |
| 未闭合 `<TOOL_CALL name="x">...` | 流式截断残留 |
| 孤立 `</TOOL_CALL>` | 标签碎片 |
| `TOOL_CALL: xxx ...` | KV 写法残留 |
| ` ```tool_call ` 代码块 | 围栏残留 |
| `[TOOLS]...[END_TOOLS]` | 提示词被模型复述 |

**无条件执行**（即使最终没有有效调用）——否则复述的标签会原样显示给用户。

---

## 8. 多轮序列化规则

`serialize_tool_turns()` 把含工具调用的 messages 转成 `[(role, text)]`：

| 输入消息 | 转成 |
|---|---|
| assistant + `tool_calls` | `[工具调用] name({args})` |
| `role="tool"` | `[工具结果] 工具名: 内容` |
| Anthropic user 里的 `tool_result` block | `[工具结果] 工具名: 内容` |
| Anthropic assistant 里的 `tool_use` block | `[工具调用] name({args})` |
| 普通 user/assistant/system | 原样 |

**工具名优先于 id**：内部维护 `tool_call_id → 工具名` 映射，
结果显示**工具名**而不是 `toolu_xxx` / `call_xxx_0`，上游模型更容易理解上下文。

---

## 9. 配置

`config.json` → `mimo` 段：

```jsonc
{
  "mimo": {
    "max_tools": 32,                   // 注入提示词的工具上限
    "empty_content_fallback": true     // 正文为空时用思考内容兜底
  }
}
```

| 配置项 | 默认 | 说明 |
|---|---|---|
| `max_tools` | 32 | 超出则截断并提示，防止提示词爆炸 |
| `empty_content_fallback` | true | 思考型模型有时把答案全写在 reasoning 里导致正文为空 → 客户端空白。开启后正文空时用思考内容补位 |

---

## 10. 已知限制

| 限制 | 说明 |
|---|---|
| **模拟实现** | 上游无原生 function calling，靠文本模拟。极端情况下模型可能不按格式输出 |
| **并行调用上限** | 单次 8 个 |
| **工具数量上限** | 提示词注入 32 个（更多会截断） |
| **schema 校验宽松** | 缺必填参数**不报错**（文本模型强行拒绝只会更糟）；有 default 时自动补 |
| **Responses 的 `id` 仍随机** | `fc_{random}`，但 `call_id` 是确定性的，回传请用 `call_id` |

---

## 11. 排障

### 客户端显示空白 / 不回复

1. **先看是不是误判** —— 开 debug 模式看上游原文，模型是不是在正常回答但被当成了调用。
   白名单已防住绝大多数情况。
2. **工具太多** —— 超 32 个会被截断，模型看不到完整列表。调大 `max_tools` 或减少工具。
3. **提示词过长** —— 工具 + 长历史叠加可能顶到上游上限。可把 `history_mode` 设为 `last`。

### 客户端显示"AI 把工具调用念出来了"

正文里出现 `<function=...>` / `<TOOL_CALL ...>` 原文。

- 流式场景：交给 `ToolTextGuard` 处理（见 6.5），已内置
- 非流式场景：说明提取失败，开 debug 看模型原始输出是哪种格式；
  若是本文档没列的新格式，在 `tools.py` 的 `RE_*` 里补一条即可
- 若工具名不在客户端传的 `tools` 里，白名单会拒绝 —— 检查客户端是否真的发了工具定义

### 工具调用没被识别

- 检查工具名是否拼错（大小写 / 后缀可容错，但完全不同的名字不行）
- 开 debug 看模型实际输出了什么文本
- 模型可能压根没输出调用块 —— 这是模型行为，代理无法强制

### 多轮读不到工具结果

- 确认客户端第二轮的 `messages` 里**同时**带了 assistant 的 `tool_calls` 和 `role="tool"` 消息。
  只发 `role="tool"` 而丢了前一条 assistant，代理无法建立 id→名称映射。

### 开 debug

`config.json` → `misc` 段：

```jsonc
{
  "misc": {
    "debug": true,           // 记录完整上游请求/响应（敏感字段自动打码）
    "debug_file": "",        // 留空只进内存日志；填路径则单独落文件
    "debug_max_body": 8000   // 单条 body 最大记录长度
  }
}
```

也可在管理面板开关，**不用重启**。

每次上游交互记录：URL、请求头、请求体、响应状态、响应体。
**敏感字段自动打码**（登录凭证、令牌、签名、手机号、邮箱全换成 `***`），正常中文不受影响。

---

## 12. 代码位置

| 文件 | 职责 |
|---|---|
| `mimo_proxy/tools.py` | 注入 / 提取 / 序列化 / 清理（本文档主体） |
| `mimo_proxy/mimo.py` | 调 `inject_tools_prompt` + `serialize_tool_turns` 构建上游请求 |
| `mimo_proxy/server.py` | 三个端点调 `extract_tool_calls` 构造 `tool_calls` |
| `mimo_proxy/convert.py` | Anthropic / Responses 响应转换与残留清理 |

主要函数：

```
tool_names(tools)              工具名集合
tool_schemas(tools)            {名: parameters}
match_tool_name(...)           工具名映射（防误判核心）
inject_tools_prompt(...)       提示词注入
extract_tool_calls(...)        提取（5 策略 + 白名单）
serialize_tool_turns(...)      多轮序列化
strip_tool_residue(...)        残留清理
```


## 13. 客户端自带原生工具协议时的透传（重要）

部分客户端（deepseek-harness）**不把 tools 放进请求体**，而是用自己的
system prompt 描述原生 `<TOOL_CALL name="..">` 协议。这时代理必须完全
不介入：

| 客户端声明 tools | 代理行为 |
|---|---|
| 有 | 注入提示词 -> 提取为 `tool_calls` -> 清理正文残留 |
| 无 | **原样透传** content，不提取、不清理 |

★ 之前的 bug：无论客户端有没有声明 tools，`strip_tool_residue()` 都会
删除正文里的 `<TOOL_CALL ...>...</TOOL_CALL>`。客户端解析不到任何工具
调用，就会反复报「工具调用失败，请重新判断」——模型换多少种写法都没用，
因为写出来的调用块全被代理删掉了。

相关函数：`_strip_residue` / `_extract_calls` / `_eff_tools`（server.py），
`ToolTextGuard(enabled=...)`（tools.py，流式下同样可关闭）。

白名单取自 `_eff_tools()` = 客户端 tools + claw 内置工具，必须与注入到
提示词里的清单一致，否则 claw 自动执行提取不到。
