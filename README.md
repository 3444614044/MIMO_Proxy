# MiMo v2.6 轻量代理（模块化版）

把 OpenAI / Anthropic / Responses 请求翻译为小米 MiMo 网页端协议。

## 快速开始

```bash
pip install requests

# 1. 生成默认配置文件
python -m mimo_proxy --init-config

# 2. 编辑 config.json（填账号、改密钥）

# 3. 启动
python -m mimo_proxy
```

管理面板：<http://localhost:8081/admin>

## 命令

| 命令 | 作用 |
|---|---|
| `python -m mimo_proxy` | 启动服务 |
| `python -m mimo_proxy --init-config` | 生成默认 `config.json` |
| `python -m mimo_proxy --show-config` | 打印当前生效配置 |
| `python -m mimo_proxy --config PATH` | 指定配置文件 |

配置文件查找顺序：`--config` 参数 → `MIMO_CONFIG` 环境变量 → `./config.json` → 包目录 `config.json`

## 配置项（config.json）

```jsonc
{
  "server": { "host": "0.0.0.0", "port": 8081 },

  "auth": {
    "api_key": "sk-mimo",              // 调用方用的 Key
    "require_for_models": false,       // /v1/models 是否也要鉴权
    "require_for_health": false        // /health 是否也要鉴权
  },

  "admin": {
    "user": "admin",                   // 管理面板账号（独立于小米账号）
    "pass": "admin",
    "accounts": [],                    // 多管理员，非空时优先于上面
    "session_ttl": 43200               // 会话有效期（秒）
  },

  "accounts": {
    "file": "accounts.json",           // 账号持久化文件（加密）
    "auto_refresh": true,              // token 失效自动重登
    "list": [                          // 账号池
      { "user": "小米邮箱/手机号", "pass": "密码" },
      { "ph": "xxx", "token": "yyy", "uid": "zzz" }
    ],
    "ph": "", "token": "", "uid": ""   // 单账号快捷写法（list 为空时生效）
  },

  "mimo": {
    "base": "https://aistudio.xiaomimimo.com",
    "model_map": { "mimo-v2.5-pro": "mimo-v2.6-pro" },
    "timeout": 120
  },

  "tts": {
    "voice": "冰糖",                   // 音色（中文名）
    "scene": "BRIEF_DESCRIPTION",
    "format": "wav",
    "voice_map": { "alloy": "冰糖" },  // OpenAI 音色名映射
    "conversation_ttl": 1800
  },

  "asr": {
    "model": "mimo-v2.5-asr",
    "language": "auto",
    "max_bytes": 10485760
  },

  "misc": { "max_logs": 200 }
}
```

### 环境变量（可选兼容）

旧的环境变量仍然可用，优先级 **高于** config.json。
设 `ALLOW_ENV_OVERRIDE=false` 可强制只用 config.json。

支持：`PORT` `API_KEY` `AUTO_REFRESH` `ADMIN_USER` `ADMIN_PASS`
`ADMIN_SESSION_TTL` `ACCOUNTS_FILE` `MIMO_PH` `MIMO_TOKEN`
`MIMO_UID` `MIMO_TTS_VOICE` `MIMO_TTS_SCENE` `MIMO_TTS_FORMAT`
`MIMO_ACCOUNTS`（JSON）`ADMIN_ACCOUNTS`（JSON）

## 模块结构

```
mimo_proxy/
├── __init__.py     包入口
├── __main__.py     命令行入口
├── config.py       配置加载（config.json + env 覆盖）
├── utils.py        日志 / 统计 / 加密 / ID 生成
├── accounts.py     Account 与 AccountPool（登录、轮询、持久化）
├── mimo.py         上游 HTTP 客户端、上传、chat
├── tools.py        工具调用注入与提取
├── speech.py       TTS / ASR（异步任务队列）
├── convert.py      Anthropic / Responses / OpenAI chunk 转换
├── admin.py        管理面板鉴权
├── panel.py        管理面板 HTML
└── server.py       HTTP 服务与启动
```

## 端点

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 对话（含流式、工具调用、多模态） |
| `POST /v1/messages` | Anthropic Messages |
| `POST /v1/responses` | OpenAI Responses |
| `GET  /v1/models` | 模型列表 |
| `POST /v1/audio/speech` | TTS 语音合成 |
| `POST /v1/audio/transcriptions` | ASR 语音识别 |
| `GET  /admin` | 管理面板 |
| `GET  /health` | 健康检查 |

管理 API（需登录）：`/api/admin/login` `logout` `session` `stats` `config` `accounts` `logs`

## 调用示例

```bash
# 对话
curl http://localhost:8081/v1/chat/completions \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.6-pro","messages":[{"role":"user","content":"你好"}]}'

# Anthropic（Claude Code / RikkaHub）
curl http://localhost:8081/v1/messages \
  -H "x-api-key: sk-mimo" -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":1024,"messages":[{"role":"user","content":"你好"}]}'

# TTS
curl http://localhost:8081/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-tts","input":"你好啊","voice":"冰糖"}' --output out.wav

# ASR
curl http://localhost:8081/v1/audio/transcriptions \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-asr","file":"'$(base64 -w0 a.mp3)'"}'
```

## 账号导入（三种方式）

### 1. accounts.json 自动加载（最简单）

代理自动保存的格式，`accounts.json` 放在工作目录即生效：

```json
{
  "accounts": [
    { "user": null, "pass_enc": "", "ph_enc": "enc1:...",
      "token_enc": "enc1:...", "uid": "6897302049" }
  ]
}
```

### 2. 管理面板粘贴

- **账密**：填小米邮箱/手机号 + 密码，自动登录换 token
- **Cookie 串**：`serviceToken=xxx; userId=123; xiaomichatbot_ph=yyy`（支持多行批量）
- **JSON 批量**：整段粘贴 accounts.json 内容，支持加密与明文，一次多个

### 3. config.json 明文

```json
"accounts": { "list": [{ "ph": "xxx", "token": "yyy", "uid": "zzz" }] }
```

### 转换工具 account_tool.py

⚠️ 必须在有 `.secret_key` 的那台机器上运行。

```bash
python account_tool.py check  accounts.json   # 检查密钥是否匹配
python account_tool.py decode accounts.json   # 查看明文
python account_tool.py config accounts.json   # 生成 config.json 片段
python account_tool.py cookie accounts.json   # 生成可粘贴的 Cookie 串
python account_tool.py encode config.json     # 反向：config.json -> accounts.json
```

## 注意事项

- 这是非官方的网页端封装，依赖 Cookie 会话，存在被风控的可能
- 公网部署务必改掉默认密钥并配 HTTPS
- 账号文件 `accounts.json` 与 `.secret_key` 必须一起备份，只拷一个解不开
- TTS/ASR 要求会话先 `save` 入库，代理已自动处理


## HTTPS 误连提示

客户端（如 rikkahub）若把 Base URL 配成 `https://...`，而代理只监听 HTTP，
日志会出现一串二进制乱码 + 400。现在会识别 TLS ClientHello 并给出中文提示。

确实需要 HTTPS 时，在 config.json 里配置：

```jsonc
"server": {
  "host": "0.0.0.0",
  "port": 8081,
  "read_timeout": 30,
  "tls": { "cert": "/path/fullchain.pem", "key": "/path/privkey.pem" }
}
```

证书文件存在且可加载时自动启用 HTTPS，否则回退 HTTP 并在启动日志里说明。

### 实现位置

TLS 拦截放在 `Handler.handle_one_request()` 里，用 `MSG_PEEK` 窥探 socket 首字节
（`0x16 0x03`）。不放 `parse_request()` 是因为 `handle_one_request()` 会先
`readline()`，而真实 TLS 流里没有 `\n`，要等客户端放弃才看到 EOF，线程会被
长时间占用。`parse_request()` 保留为兜底，且只看 `raw_requestline`，
不再自己 `readline()`（否则会把 Header 当请求行，导致所有请求 400）。

## 注意：内嵌 HTML 必须用 raw 字符串

`panel.py` 里的 `ADMIN_HTML` 是 **raw 三引号字符串**。若改回普通三引号，
JS 中写的 `'\n'` 会被 Python 当转义吃掉变成真换行：

```js
// 想要的效果
msg += '\n\n💡 ' + r.hint;
// 普通三引号下 Python 输出（JS 单引号内不能有裸换行 → SyntaxError）
msg += '

💡 ' + r.hint;
```

结果 `/admin` 仍返回 200，但整个 `<script>` 块失效，页面点什么都没反应。
改完记得跑 `python test_panel_html.py`。

## 账密添加失败：Invalid URL ''

小米在「需要验证码 / 二次验证 / 风控拦截」时，`serviceLoginAuth2` 返回的
`location` 是**空字符串**，直接发请求会抛：

```
Invalid URL '': No scheme supplied. Perhaps you meant https://?
```

现在会给出可读提示：*登录未完成：响应里没有跳转地址……通常是账号需要验证码
或二次验证*。

**解决办法**：先用浏览器在 <https://account.xiaomi.com> 完整登录一次
（把验证码走完），再从浏览器复制 Cookie，用管理面板的「Cookie 导入」添加。

## 启用 HTTPS 与 Android 信任证书

客户端报 `CertPathValidatorException: Trust anchor for certification
path not found.` —— 说明握手成功但客户端信任库里没有这张自签证书的根。

```bash
python setup_https.py gen --ip 192.168.0.189 --dns localhost
python setup_https.py android cert.pem    # 输出 Android 安装命令
python setup_https.py off                 # 如何退回 HTTP
```

要点：
- 证书 **必须带 SAN**（IP 或 DNS）；有 SAN 时 Android 完全忽略 CN。
- Android 7(API 24)+ 起，App 默认**不信任用户 CA**，装到「用户凭据」
  多半仍报错，需要放进 `/system/etc/security/cacerts/`（root/Magisk）。
- 不需要 HTTPS 就直接删掉 `server.tls`，客户端改回 `http://`，最省事。

## 真实公网证书（局域网也适用）

证书绑定的是**域名**，不是 IP 也不是端口 —— 所以代理只跑在内网
`192.168.0.189:8081` 也能用真实证书，只要客户端能把这个域名解析到内网 IP，
流量仍走局域网，不必绕公网。

```bash
python setup_public_cert.py guide --domain mimo.example.com --provider cloudflare
python setup_public_cert.py check  --domain mimo.example.com --ip 192.168.0.189
python setup_public_cert.py apply  --cert ~/mimo_proxy/fullchain.pem \
                                   --key  ~/mimo_proxy/key.pem
python setup_public_cert.py verify --domain mimo.example.com
```

要点（都是实测确认过的）：
- 用 **DNS-01** 挑战：不需要公网 IP，不需要开 80/443。
- 代理必须用 **fullchain.pem**（含中间证书）。只给 `cert.pem` 时
  Android 依然可能报 `Trust anchor for certification path not found`。
- **必须用域名访问**。证书 SAN 里没有 IP，用 `https://192.168.0.189:8081`
  会报 `IP address mismatch`（已实测复现）。
- 证书不绑定端口，`https://域名:8081` 完全合法。
- `verify` 会检查：证书数量、SAN、是否自签、私钥匹配、有效期。

## Cloudflare 专用一键脚本

```bash
DOMAIN=mimo.example.com PROXY_IP=192.168.0.189 bash setup_cf.sh dns
DOMAIN=mimo.example.com bash setup_cf.sh tunnel
DOMAIN=mimo.example.com bash setup_cf.sh check
```

| | `dns`（DNS-01） | `tunnel` |
|---|---|---|
| 客户端证书 | Let's Encrypt，系统信任 | CF 签发，系统信任 |
| 流量 | **走局域网** | 绕 Cloudflare 公网 |
| 需要 | 域名 + API Token + 自己续期 | 域名 + cloudflared |
| 客户端地址 | `https://域名:8081` | `https://域名`（标准 443） |

**`dns` 模式最容易踩的坑**：Cloudflare 上那条 A 记录的 Proxy status
必须是 **DNS only（灰色云）**。开橙色云的话，域名解析到的是 Cloudflare 的
IP（不是你的内网 IP），客户端在局域网里也会被送去公网绕一圈；而且 CF 只放行
固定几个端口，8081 会被挡。灰色云 = 纯 DNS 解析，客户端拿到内网 IP 直连。

另外必须用**域名**访问，不能用 IP：`https://192.168.0.189:8081` 会报
`IP address mismatch`，因为证书 SAN 里只有域名（已实测复现）。

## ASR 功能

`POST /v1/audio/transcriptions`，完整兼容 OpenAI：

| 字段 | 说明 |
|---|---|
| `file` | base64 / `data:` URL / `http(s)` URL |
| `language` | `auto` / `zh` / `en`…，请求级优先于配置 |
| `prompt` | 传给上游的提示词 |
| `response_format` | `json` / `text` / `verbose_json` / `srt` / `vtt` |
| `filename` | 可选，辅助判断音频格式 |

config.json 的 `asr` 段：

```jsonc
"asr": {
  "model": "mimo-v2.5-asr",
  "language": "auto",
  "max_bytes": 10485760,     // 音频大小上限
  "max_wait": 90,            // 轮询总时长(秒)
  "poll_interval": 0.6       // 首次间隔，之后指数退避至 2s
}
```

实现要点（都是踩过的坑）：
- 上传后 `audioUrl` 必须填 `resourceUrl` 而**不是** `uploadUrl`
  （两者签名/有效期不同，填错是 500）。
- 文件名扩展名按 **magic bytes** 嗅探（wav/mp3/flac/ogg/m4a/webm），
  认不出时用 `filename` 兜底。RIFF 会再确认是 `WAVE`，避免把 AVI 认成 wav。
- 上游只返回 `data.text`，**没有 segments / duration**，所以 `srt` / `vtt`
  只能生成整段一条的字幕，`verbose_json` 的 `segments` 为空——不编造时间戳。
- 轮询用指数退避（0.6s 起步，上限 2s），短音频不用白等 1 秒。

回归：`python test_asr_enhance.py`

## chat 三个常见问题（已修）

**① 只有思考、没有正文**

上游思考结束后会继续发 `{"reasoningContent": "", "content": "你"}`。
旧代码用 `"reasoningContent" in data` 判断 —— 键还在就走 reasoning 分支，
返回空串后被 `if not val: continue` 丢掉，**正文整段消失**。
现在按"非空"判断，且一个事件同时含两者时两个都产出。

**② 没有上下文**

`build_mimo_payload` 里 `user_query = content` 是覆盖赋值，只留最后一条，
历史全丢；`conversationId` 每次也新建。现在：
- 历史拼进 `query`（`[对话历史]` 块），默认 `history_mode=full`
- 会话可复用：请求头 `X-Mimo-Conversation-Id` > 请求体 `user` 字段

**③ 容易断**

深度思考时上游几十秒不发数据。旧代码 `for line in iter_lines()` 一直阻塞，
既发不出心跳也判不了超时。现在后台线程读、主线程带 1s 超时取队列，
静默期间每秒醒来发 `: ping` 并检查空闲超时。

config.json 新增：

```jsonc
"mimo": {
  "stream_idle_timeout": 300,   // 上游多久无数据就放弃(秒)
  "heartbeat_interval": 15,     // 流式心跳间隔(秒)，0=关闭
  "history_mode": "full",       // full拼全量历史 | last只发最后一条 | auto
  "conversation_ttl": 1800      // 会话复用有效期(秒)
}
```

回归：`python test_chat_fix.py`

## 依据 ProxyPin 抓包（2026-10-23）的更新

**1. temperature / topP 之前没传给上游（真 bug）**

抓包实证上游 `modelConfig` 是：

```json
{"enableThinking":false,"model":"mimo-v2.6-pro-ultraspeed-studio",
 "temperature":0.8,"topP":0.95,"webSearchStatus":"disabled"}
```

代理原来只发 `enableThinking` / `model` / `webSearchStatus` 三个字段，
**客户端设 temperature / top_p 完全不生效**。现已透传，并做范围校验
（temperature 0~2，top_p 0~1），非法值静默忽略而不是报错。

**2. 新模型 `mimo-v2.6-pro-ultraspeed-studio`**

网页端 fastchat 通道实测在用的模型，已加入 `/v1/models`。
别名 `mimo-v2.6-ultraspeed` 也映射到它。

**3. 联网搜索开关（默认关，与网页端一致）**

```python
{"web_search": true}                       # 请求体
X-Mimo-Web-Search: enabled                 # 请求头
"mimo": {"web_search": false}              # config.json
```

**4. 配额查询（新增）**

```
GET /open-apis/user/mimo-claw/status      -> 免费额度、集群、过期时间
GET /open-apis/user/mimo-claw/quotaUsage  -> 存储用量
```

管理面板新增「📦 配额（mimo-claw）」卡片，点「刷新配额」查看每个账号的
剩余额度 / 用量百分比 / 集群 / 存储。后端接口 `GET /api/admin/quota`。

实测你那个账号：FREE · 2000 万 tokens · 剩余 100% · 集群 OVERSEA ·
存储 50 GB（已用 1.3 MB）。

**5. 会话列表端点确认**

```
POST /open-apis/chat/conversation/list
body: {"queryParam":{"type":"chat"},"pageInfo":{"pageNum":1,"pageSize":128}}
```

抓包里传的是 `type:"claw"`（mimo-claw 沙箱会话），普通聊天要传 `"chat"`。
接口 `GET /api/admin/conversations`。

**6. 抓包里的其他端点（未接入，仅记录）**

| 端点 | 用途 |
|---|---|
| `POST /fastchat/open-apis/bot/chat` | fastchat 通道 chat（备用） |
| `POST /fastchat/open-apis/chat/conversation/save` | 会话入库 |
| `POST /fastchat/open-apis/chat/dialog/list` | 历史消息 |
| `GET /open-apis/user/ws/ticket` + `wss://.../ws/proxy?ticket=` | WebSocket |
| `GET /open-apis/host-files/list` `POST .../download` `.../preview` | mimo-claw 沙箱文件 |

回归：`python test_har_update.py`

## 错误诊断误报修复（重要）

**现象**：导入 Cookie 后返回

```json
{"error":{"message":"listindexoutofrange  <凭证含 4 个非法字符，请重新导入>"}}
```

**真相**：真实异常是 `IndexError("list index out of range")`。
`_err_str()` 拿异常信息去 `sanitize_cookie_value()` 清洗，而 Cookie
白名单**不含空格**，于是 4 个空格被剔除并计为"非法字符"：

```
"list index out of range"  ->  "listindexoutofrange" + bad=4
```

结果把普通 IndexError 伪装成"凭证坏了"，用户只能反复重新导入
（而凭证其实是好的）。同类受害的还有：

```
"Invalid URL '': No scheme supplied."  ->  "含 8 个非法字符"
```

**修复**：新增 `has_binary()`，只统计控制符与 Latin-1 高位区
(0x80-0xFF，解密失败的典型乱码)。空格、标点、中文、emoji
都算正常字符。

同时：
- 兜底异常记录完整 traceback（最后两帧）到日志
- Cookie 导入分支包 try，`test_account` 抛异常不再冒泡成 500
- `path.split("/")[4]` 加长度防御

回归：`python test_err_diag.py`

## 压缩包内容

只含运行所需文件，**不含测试脚本**：

```
mimo_proxy/          代理主包（12 个模块）
config.json          配置
README.md
account_tool.py      账号导入/导出/转换工具
check_refs.py        跨模块引用自检
setup_https.py       自签证书助手
setup_public_cert.py 公网证书助手
setup_cf.sh          Cloudflare 一键脚本
```

各功能的回归测试脚本留在开发目录，不随包发布；
需要自检时可单独取用（test_err_diag.py / test_chat_fix.py /
test_event_type.py / test_stream_timing.py / test_abort.py /
test_asr_enhance.py / test_panel_html.py / test_tls_*.py /
test_login_diag.py / test_har_update.py / test_e2e_cn.py）。

## AstrBot 接入

AstrBot 用 `openai_chat_completion` provider，走标准 OpenAI SDK，
直接指向本代理即可。

### 配置

WebUI → **模型提供商** → **对话** → 新增 → 选 **OpenAI Compatible**：

| 字段 | 值 |
|---|---|
| API Base URL | `http://<手机IP>:8081/v1` |
| API Key | `sk-mimo`（或你在 config.json 里改的 `auth.api_key`） |
| 模型 | 点「获取模型列表」选一个，或手填 `mimo-v2.6-pro` |

或写进 `data/cmd_config.json`：

```jsonc
{
  "provider": [{
    "id": "mimo",
    "type": "openai_chat_completion",
    "model": "mimo-v2.6-pro",
    "key": ["sk-mimo"],
    "api_base": "http://192.168.0.189:8081/v1",
    "enable": true
  }],
  "provider_settings": { "default_provider_id": "mimo" }
}
```

然后到「配置文件」→ AI 配置 → 模型 → 把**对话模型**设为刚加的，
**点右下角保存**（改完不保存不生效）。

### 模型名容错

WebUI 里模型名可以手填。填了上游不认识的名字（比如 `gpt-4o`、
`deepseek-chat`、随手起的别名）时，默认**自动回退**到
`mimo.default_model`，而不是把错误名字丢给上游导致整条链路报错。

```jsonc
"mimo": {
  "default_model": "mimo-v2.6-pro",
  "on_unknown_model": "fallback"   // fallback | passthrough | error
}
```

| 策略 | 行为 |
|---|---|
| `fallback`（默认） | 回退到 `default_model`，日志提示 |
| `passthrough` | 原样发给上游（上游可能拒绝） |
| `error` | 直接返回 400 并列出可用模型 |

已知模型名（`mimo-v2.6-pro` 等）始终原样透传，不受影响。

### 不支持：知识库（embeddings）

AstrBot 的**知识库**功能依赖 `/v1/embeddings`，MiMo 上游没有向量接口。
本代理对这类端点返回 OpenAI 标准错误 JSON（不是空 404），
错误信息里会直接说明原因和替代方案。

要用知识库，在 AstrBot 里**单独配一个 embedding 提供商**
（SiliconFlow / 阿里云百炼等都支持 OpenAI 格式），
或者关掉知识库相关插件。对话、工具调用、多模态不受影响。

同理返回标准错误的还有：`/v1/moderations`、`/v1/images/*`、
`/v1/rerank`、`/v1/completions`。

### 已验证的能力

用真实 `openai` SDK 跑通（`python test_astrbot.py`，8 组 30 项）：

| 能力 | 状态 |
|---|---|
| 模型列表（`client.models.list()`） | ✅ |
| 非流式对话（AstrBot 默认） | ✅ |
| 流式 + `stream_options.include_usage` | ✅ |
| 工具调用（MCP / 插件 / Agent） | ✅ |
| 多轮工具结果回传 | ✅ |
| 多模态（图片） | ✅ |
| 未知模型名回退 | ✅ |
| embeddings 明确报错 | ✅ |

### 流式输出

AstrBot 的流式要在**配置文件**里单独开：
AI → 通用设置 → 消息处理 → 打开「流式输出」，然后保存。
（`provider_settings.streaming_response` 默认为 false。）

### 排障

AstrBot 日志里出现连接类错误时，先确认：

1. 手机与 AstrBot 在同一局域网，且代理在跑
   （`curl http://<手机IP>:8081/v1/models`）
2. Base URL 是 `http://` 不是 `https://`（配成 https 会被当成 TLS 握手，
   代理日志里会打出一堆乱码 —— 见「HTTPS 误连提示」）
3. 开 debug 模式看上游原文：`config.json` → `misc.debug: true`

### mimo-claw 沙箱

mimo-claw 是小米给账号分配的云端沙箱容器（含 token 额度与存储配额）。
代理把它做成两组能力。

**管理面板**（`🐾 沙箱容器` 卡片）：

| 按钮 | 作用 |
|---|---|
| 查容器 | 状态 / 集群 / 剩余额度 |
| 申请容器 | create，并自动轮询到 AVAILABLE |
| 保障可用 | 无容器时自动申请并等待就绪 |
| 释放 / WS 票据 / 沙箱会话 / 读消息 | 对应端点 |

对应 API：

```
GET  /api/admin/claw                       容器状态（?wait=1 轮询到就绪）
POST /api/admin/claw/create                {uid?, wait:true}
POST /api/admin/claw/ensure                {uid?, auto_create:true}
POST /api/admin/claw/release               {uid?}
GET  /api/admin/claw/ws?uid=               WebSocket 票据
GET  /api/admin/claw/conversations         沙箱会话列表
GET  /api/admin/claw/dialog?conversationId=   会话历史消息
GET  /api/admin/claw/quota                 所有账号配额汇总
```

**把沙箱暴露成 OpenAI 工具**（`claw.expose_tools: true`）：
追加 6 个内置工具 —— `claw_container_status` / `claw_list_files` /
`claw_read_file` / `claw_list_sessions` / `claw_read_session` /
`claw_ws_ticket`。模型可像调普通工具一样调用。

再开 `claw.auto_execute: true` 时，**代理会自己执行这些工具并把结果
回灌给上游**，客户端拿到的就是最终答案，不需要实现任何东西
（否则客户端没有对应实现，执行失败会白跑一轮）。

配置项：

```jsonc
"claw": {
  "host_files_prefix": "/open-apis/user/mimo-claw/host-files",
  "release_path": "/open-apis/user/mimo-claw/release",
  "auto_create_on_start": false,   // 启动时为无容器的账号自动申请
  "create_wait_timeout": 90,
  "ensure_cache_ttl": 120,         // 容器状态缓存秒数，0=不缓存
  "expose_tools": false,
  "auto_execute": false,
  "auto_execute_rounds": 3
}
```

⚠️ `host_files` / `release` 两组端点来自早期抓包的**名称记录**，
未完整实证。失败时会返回明确错误与提示，不会静默成功。
要用稳的话，在浏览器里操作一次沙箱并抓包，把真实路径填进
`host_files_prefix` / `release_path`。

### DeepSeek Harness (dsh)

dsh 是 agent harness，通过 OpenAI 兼容端点接模型。

Web UI：Settings → Models → Add provider，选 OpenAI 兼容，填：

```
Base URL:  http://192.168.0.189:8081/v1
API Key:   sk-mimo
Model:     从列表选，或填 mimo-v2.6-pro
```

Python SDK：`export DEEPSEEK_BASE_URL=http://192.168.0.189:8081/v1`

已做的适配：

| 要点 | 说明 |
|---|---|
| `developer` role | 归一成 `system`，否则 dsh 的系统指令会被整体丢弃 |
| 工具调用循环 | bash / str_replace_editor 多轮调用与结果回传 |
| `parallel_tool_calls:false` | 提示模型一次只调一个工具 |
| 未知模型名 | dsh 发 `deepseek-v4-flash` 等时回退默认模型 |
| `stream_options.include_usage` | usage chunk 按 OpenAI 规范（`choices: []`）发出 |

若流式输出有问题，按上一节开强制非流式，或把
`force_nonstream_ua` 设为 `["dsh"]`。

### 强制非流式

部分客户端（AstrBot）对逐字流式处理有问题 —— 半截标签、工具调用 XML
会先显示出来。可开启"攒齐后一次性发完"：

```jsonc
"server": {
  "force_nonstream": true,            // 全局开启
  "force_nonstream_ua": ["AstrBot"]   // 或只按 User-Agent 匹配
}
```

也可以按请求开启，加请求头 `X-No-Stream: 1`。

注意：仍返回 SSE 封装（客户端 `stream: true` 需要 SSE 格式），
只是不再逐字推送，响应头会带 `X-Mimo-Stream: oneshot`。

三个端点（chat/completions、messages、responses）都生效。

### deepseek-harness（dsh）

DSH 支持 OpenAI-compatible provider，直接填：

```
Base URL: http://你的IP:8081/v1
API Key:  sk-mimo
Model:    mimo-v2.6-pro
```

已适配的差异点：

| 项目 | 处理 |
|---|---|
| `stream_options.include_usage` | usage 单独一个 chunk，`choices` 为空数组，位于 `finish_reason` 之后（OpenAI 规范） |
| `developer` role | 归一为 `system`（上游只认 system/user/assistant） |
| `stop` | 上游不支持，代理侧软截断；流式下用前置过滤，不会先把半截 stop 序列发出去 |
| `max_tokens` / `service_tier` / `logprobs` 等 | 上游不支持，静默忽略不报错 |

### mimo-claw 沙箱

面板「配额与容器」卡片可查额度、查容器、申请容器。

```bash
# 查所有账号容器状态（wait=1 会在 CREATING 时轮询到就绪）
curl -H "Authorization: Bearer $TOK" \
  "http://127.0.0.1:8081/api/admin/claw?wait=1"

# 申请容器
curl -X POST -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -d '{"wait":true}' http://127.0.0.1:8081/api/admin/claw/create

# claw 沙箱会话列表（type=claw，与普通 chat 会话分开存）
curl -H "Authorization: Bearer $TOK" \
  http://127.0.0.1:8081/api/admin/claw/conversations

# 沙箱文件（端点路径可调）
curl -H "Authorization: Bearer $TOK" \
  "http://127.0.0.1:8081/api/admin/claw/files?action=list"
```

配置：

```jsonc
"claw": {
  "host_files_prefix": "/open-apis/user/mimo-claw/host-files",
  "auto_create_on_start": false,   // 启动时自动为无容器账号申请
  "create_wait_timeout": 90
}
```

⚠️ `host-files` 这组端点来自早期抓包的**名称记录**，未完整实证。
若返回错误，用浏览器抓一次沙箱文件操作，把真实路径填进
`claw.host_files_prefix` 即可，其余 claw 端点均有抓包实证。

### 工具调用被"念出来"

如果 AstrBot 里 AI 把 `<function=astrbot_execute_shell><parameter=command>...`
整段显示在消息里，说明模型用了其它框架的工具调用格式。

已支持两种风格（会自动识别）：

```
<TOOL_CALL name="x">{"a":1}</TOOL_CALL>                  ← 本代理注入的格式
<function=x><parameter=a>1</parameter></function>        ← AstrBot / llama 风格
```

流式下还会先把疑似工具调用的片段压住，确认是工具调用后才发
`tool_calls`，不会把 XML 先显示给用户。详见 `function.MD` 第 6 节。

## 流式空响应修复（客户端一片空白）

**现象**：rikkahub 不显示任何回复，代理日志却是

```
stream ok model=mimo-v2.6-pro reasoning=False content=False chunks=0
```

**根因**：两个掩盖真相的坑叠加。

1. `call_mimo_stream()` 里 `r.raise_for_status()` 被 `try/except pass`
   吞掉。上游返回 401 / 403 / 500（风控页、裸 JSON 错误）时照常往下走，
   响应体里没有 `data:` 前缀 → 解析出 0 个事件 → 静默 `[DONE]`
   → 客户端一片空白。
2. 日志无条件写 `stream ok`，`chunks=0` 看起来完全正常。

**修复**：

- HTTP 非 2xx **在启动后台线程之前**检查，附状态码 + 响应体前 400 字符
  （放在线程启动后会和 reader 抢同一个流，导致错误时有时无）
- 上游直接返回裸 JSON（非 SSE）时也解析成事件
- 一个事件都没产出时抛错，附上游原文
- `chunks=0` 时日志标 ⚠️ 而不是 ok
- 队列解包改用 `item`，不再遮蔽请求体参数 `payload`

回归：`python test_stream_empty.py`（含 403 / 裸 JSON / 空响应 / 正常 SSE 四种场景）

## 上游直连诊断工具

代理报 `stream 空响应 / chunks=0` 时，用它直连上游看真实响应，
**不用抓包**（抓包容易漏掉关键请求）。

```bash
python diag_upstream.py                      # 测账号池里所有账号
python diag_upstream.py --uid 6897304740     # 只测指定账号
python diag_upstream.py --model mimo-v2.6-flash
python diag_upstream.py --no-think           # 关闭深度思考
```

照代理的方式构造请求（相同的头与 payload 结构），打印 HTTP 状态码、
响应体前 600 字符，并给出结论：

| 上游返回 | 结论与处理 |
|---|---|
| SSE 含 content | ✅ 上游正常，代理仍为空则属解析问题 |
| `code:401` / `loginUrl` | 凭证过期 → 重新导入 Cookie |
| `code:403` / 含"风控" | 账号被风控 → 换账号或等 |
| HTTP 非 200 | 网关拦截 |
| 空响应体 | 连接被重置 / 超时 |
| 裸 JSON（非 SSE） | 接口改版或模型不被接受 |

逐个测所有账号并汇总哪些可用。

## 抓包的注意事项

抓 MiMo chat 请求时：

1. F12 → Network → 🚫 **Clear** 清空
2. 勾选 **Preserve log**（防止跳转丢失）
3. 过滤框输入 `react/chat`
4. **再**发消息

否则抓到的多半是 Chrome 扩展更新、统计脚本、图片下载等无关流量。

## HAR 2026-10-24 抓包更新

### 1. 空响应真凶：上游 error 事件被吞（核心）

抓包铁证 —— `/fastchat/open-apis/chat/dialog/list` 返回：

```json
{"result":"服务器繁忙，请稍后再试","dialogStatus":"processing"}
```

上游繁忙/风控时返回 error 事件，而代理三处端点都是：

```python
if kind == "error":
    log(...)      # 只写日志
    continue      # ← 吞掉，客户端什么都收不到
```

客户端收到 0 chunk + `[DONE]` → **整屏空白**，日志还写 `stream ok chunks=0`。

**修复**：新增 `_upstream_err_text()`，把错误转成 `[上游提示] xxx` 当正文发给客户端。
三个端点（OpenAI / Anthropic / Responses）全部接入。

### 2. 会话只在 fastchat 通道

| 端点 | 结果 |
|---|---|
| `POST /open-apis/chat/conversation/list` | `total: 0` |
| `POST /fastchat/open-apis/chat/conversation/list` | `total: 2`（有数据） |

`list_conversations()` 改为**双通道查询并合并去重**。
新增 `list_dialogs()`，body 必须是双层包装：

```json
{"queryParam":{"conversationId":"...","endId":"..."},
 "pageInfo":{"pageNum":1,"pageSize":10}}
```

早期直接发 `{"page":1,"pageSize":20}` 会 500。

### 3. GET /open-apis/bot/config —— 权威模型清单

新增 `fetch_bot_config()`，返回 15 个官方模型及默认参数：

```
mimo-v2.6-flash / mimo-v2.6-pro / mimo-v2.6-pro-ultraspeed-studio
mimo-v2-flash / mimo-v2-pro / mimo-v2-omni / mimo-v2.5-pro / mimo-v2.5
mimo-v2-flash-studio / clawm-alpha / clawl-alpha
mimo-v2.1-omni / mimo-v2.1-omni-preview / mimo-v2.1-pro / mimo-v2.1-pro-preview
```

每项含 `temperature`(0.8) / `topP`(0.95) / `thinkingDefaultOn` /
`webSearchDefaultStatus` / `fileUpload` 限制 / `nParam` / `nToken`。
另有 `ttsVoices`（音色清单）与 `ext`（功能开关）。

`config.json` 的 `mimo.model_ids` 已按此扩充到 17 项。

### 4. 其他实证端点

```
POST /fastchat/open-apis/chat/dialog/metrics/user/get   TPS 指标
GET  /open-apis/user/mi/get                             用户信息
```

图片上传 `multiMedias` 的 `fileUrl` 是 CDN 签名地址。

回归：`python test_har_1024.py`

## 管理接口 /api/admin/accounts

> 注意路径是复数 `accounts`，不是 `account`。

### 鉴权

先登录拿 token，之后用 Bearer 带上：

```bash
TOK=$(curl -s http://127.0.0.1:8081/api/admin/login   -H 'Content-Type: application/json'   -d '{"user":"admin","pass":"你的密码"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl http://127.0.0.1:8081/api/admin/accounts -H "Authorization: Bearer $TOK"
```

也支持 `X-Admin-Token: $TOK` 头，以及旧式 `Authorization: Basic base64(用户:密码)`。
未登录返回 `401 {"error":"unauthorized","needLogin":true}`（不发 `WWW-Authenticate`，避免浏览器弹原生框）。

### 三种添加模式

**模式 A：小米账密**（有 user+pass 时优先）

```jsonc
{"user":"小米邮箱或手机号", "pass":"密码", "ph":"可选"}
```

**模式 B：Cookie 串**（支持多行，一次导多个）

```jsonc
{"cookie": "第一行cookie\n第二行cookie"}
```

单行格式（三个字段必须有，顺序不限，值可用引号包裹）：

```
xiaomichatbot_serviceToken="xxx"; userId=123456; xiaomichatbot_ph="yyy=="
```

`serviceToken` 与 `xiaomichatbot_serviceToken` 两种写法都认。

**模式 C：accounts 数组**（明文或 `enc1:` 加密，可批量）

```jsonc
{"accounts":[{"uid":"123","token":"xxx","ph":"yyy"},
             {"user":"账号","pass":"密码"}]}
```

### 返回

```jsonc
{"ok": true,
 "added": [{"uid":"6897306279","ok":true,"error":null}],
 "failed": [],
 "uid": "6897306279",            // 兼容旧字段，取第一个
 "error": null}
```

### 本次修掉的两个真 bug

**1. Cookie 多行只导第一个，且字段串味**

原实现把整段丢给 `re.search(rf"{k}=([^;]+)")`：

- `[^;]+` 不排除换行 → 行末字段把下一行吞进来，
  实测 `ph` 变成 `PH1==
serviceToken=TOK456`
- `search` 只取首个匹配 → 第二行起的账号全部丢失

管理面板宣称"支持多行，一次导入多个账号"，实际只导第一个。
现改为按行切分 + 字符类加 `\n` 排除。

**2. last_error 泄漏 ph 明文**

requests 的异常信息含完整 URL，URL 带 `?xiaomichatbot_ph=xxx`，
面板直接展示 `last_error` → ph 明文上屏。新增 `_redact()`
统一替换成 `***`（含 URL 编码形式）。

回归：`python test_admin_accounts.py`

## MCP / tools 场景空回复修复

> 现象：**rikkahub 开启 MCP 后就不回复**。

### 根因：误判工具调用

旧的 `extract_tool_calls` **完全不知道客户端提供了哪些工具**，
只要文本里出现"像工具调用"的东西就当真。实测三类误判：

| 场景 | 模型实际输出 | 被误判成 |
|---|---|---|
| 普通回答夹带 JSON | `{"name":"my-server","arguments":{...}}` | 调用 `my-server` |
| 复述工具用法 | `<TOOL_CALL name="工具名">...` | 调用 `工具名` |
| 思考里提 TOOL_CALL | `TOOL_CALL: xxx 这种写法不对` | 调用 `xxx` |

于是 `finish_reason` 变成 `tool_calls`，rikkahub 收到后去执行这些
**根本不存在的 MCP 工具** → 执行失败 → 前端显示空白。
用户视角就是"开了 MCP 就不回复"。

### 修复

**1. 工具名白名单（核心）**

```python
names = tool_names(tools)                    # 客户端提供的工具名集合
extract_tool_calls(text, names)              # 不在白名单内 = 不算调用
```

六个调用点（OpenAI/Anthropic/Responses × 流式/非流式）全部接入。

**2. 注入提示词不再给"活"示例**

旧版把完整的 `<TOOL_CALL name="工具名">{...}</TOOL_CALL>` 写进提示词，
模型复述时会被策略 1 原样命中。改成占位写法：
`< TOOL_CALL > ... </ TOOL_CALL >`，模型看得懂但不被正则匹配。

**3. 提示词膨胀控制**

40 个 MCP 工具 → 注入 22981 字符 ≈ 7.6K tokens，叠加历史容易顶到上限。
新增：

```jsonc
"mimo": {
  "max_tools": 32,          // 工具数量上限，超出提示"另有 N 个未列出"
  "max_tool_desc": 160,     // 单条描述截断
  "empty_content_fallback": true
}
```

`parameters` 自动精简：砍长描述、限制属性数、超长只留属性名清单。
实测 40 个工具从 22981 降到 9100 字符。

**4. 空正文兜底**

思考型模型有时把答案全写进 `reasoning`、`content` 为空 → 客户端空白。
现在正文为空时用思考内容补位（流式/非流式都有），
可用 `mimo.empty_content_fallback=false` 关闭。

**5. 支持 `tool_choice`**

`"none"` 时不注入工具；`{"function":{"name":...}}` 时提示"本次必须调用"。

### 验证

```
json   : finish=stop        有正文 ✅ tools=0
recite : finish=stop        有正文 ✅ tools=0
think  : finish=stop        有正文 ✅ tools=0
real   : finish=tool_calls  有正文 ✅ tools=1   ← 真调用仍保留
only_reason: finish=stop    有正文 ✅           ← 兜底生效
```

反向验证：关掉白名单后，前三项立刻误判出 `my-server` / `工具名` / `xxx`。

回归：`python test_mcp_tools.py`

## 工具调用健全化

上游 MiMo 是纯文本单轮接口，没有原生 function calling，
工具调用靠"提示词注入 + 文本提取"模拟。健全化做了七件事。

### 1. 多轮工具结果回传（最关键）

**旧实现完全丢弃 `role="tool"` 的消息** —— 客户端执行完工具把结果
发回来，代理看都不看，模型永远不知道工具返回了什么，多轮调用直接断链。

现在 `serialize_tool_turns()` 把三类消息都序列化进上游 query：

```
用户: 查下北京天气
助手: [工具调用] get_weather({"city": "北京"})
用户: [工具结果] get_weather: 晴 25度      ← 旧实现这里整个丢失
```

同时覆盖 Anthropic 风格（`assistant` 的 `tool_use` block + `user` 的
`tool_result` block），并从 `tool_use.id` 建立映射，结果标签显示
**工具名**而不是 `toolu_xxx`。

### 2. 提取容错（模型写得潦草也要认）

| 情况 | 例子 | 结果 |
|---|---|---|
| 未闭合标签 | `<TOOL_CALL name="x">{...}` | ✅ 识别（流式截断常见） |
| 参数非 JSON | `city=北京` | ✅ 修成 `{"city":"北京"}` |
| markdown 围栏 | ```` ```json {...} ``` ```` | ✅ 剥掉 |
| 尾随逗号 | `{"city":"北京",}` | ✅ 修掉 |
| 单引号 | `{'city':'北京'}` | ✅ 转双引号 |
| 工具名大小写错 | `Get_Weather` | ✅ 映射到 `get_weather` |
| 工具名加后缀 | `get_weather_tool` | ✅ 映射 |
| 参数名错 | `{"City":...}` | ✅ 纠正到 schema 里的 `city` |
| 重复调用 | 同一调用出现两次 | ✅ 去重 |
| 不存在的工具 | `nope_zzz` | ✅ 拒绝（白名单） |

工具名匹配顺序：精确 → 大小写不敏感 → 归一化（去 `_ - . 空格`）
→ difflib 模糊 → 互为子串。匹配不上就**不算调用**。

### 3. 多工具并行

一次输出多个调用块全部提取，`index` 正确递增。

### 4. tool_call id 确定性

旧实现用 `gen_msg_id()` 现算，同一请求内每个 id 都随机。
现在基于本次响应 id + 序号：请求内唯一、跨请求可区分、排障可追踪。

### 5. 残留清理加强

未闭合标签、孤立的 `</TOOL_CALL>` 都会被清掉，不留给用户看。

### 6. 提示词瘦身（见上一节）

40 个 MCP 工具从 22981 降到 ~9100 字符。

### 7. 支持 tool_choice

`"none"` 不注入；`{"function":{"name":...}}` 提示本次必须调用。

### 顺带修的真 bug

`check_refs.py` 抓到：非流式响应里 `self._tool_call_id(cid, i)` 用了 `i`，
但列表推导写的是 `for tc in tool_calls` —— 没有 `enumerate`，
实际运行会 `NameError`（工具调用的非流式路径必崩）。

回归：`python test_mcp_tools.py`（7 组 40 项）

## TTS 音色克隆 / 音色设计（2026-10-25 抓包实证）

除了标准预设音色，小米还有两个模型，协议差别在 `content.messages[0]`
（user 位）和 `audio` 字段：

| 模式 | modelCode | user 位 | audio.voice | scene |
|---|---|---|---|---|
| 标准 | `mimo-v2.5-tts` | 语气描述（可空） | 音色名 `"冰糖"` | 有 |
| 音色克隆 | `mimo-v2.5-tts-voiceclone` | **空字符串** | **参考音频的 resourceUrl** | **无** |
| 音色设计 | `mimo-v2.5-tts-voicedesign` | **音色描述文字** | **不带 voice 字段** | **无** |

★ 最容易踩的坑：`scene` 只在标准模式出现。vc/vd 的 `modelConfig`
只有 `modelCode`，多传 scene 是错的。

### 官方音色（bot/config 的 data.ttsVoices）

| 音色 | 说明 |
|---|---|
| 冰糖 / Mia | 活泼少女 / Lively girl |
| 茉莉 / Chloe | 知性女声 / Sweet Dreamy |
| 苏打 / Milo | 阳光少年 / Sunny boy |
| 白桦 / Dean | 成熟男声 / Steady Gentle |

### 官方别名（bot/config 的 redirectTo）

```
mimo-v2.1-tts / mimo-v2.5-tts              -> mimo-v2.5-tts
mimo-v2.1-tts-vc / mimo-v2.5-tts-vc        -> mimo-v2.5-tts-voiceclone
mimo-v2.1-tts-vd / mimo-v2.5-tts-vd        -> mimo-v2.5-tts-voicedesign
```

### 用法

**音色设计**（用文字描述音色）

```bash
curl http://localhost:8081/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts-voicedesign",
    "input": "哈哈哈！说得好！天大地大，哪里不是咱们的酒桌？",
    "voice_desc": "中年男性，武侠小说侠客风，声音洪亮豪爽，带爽朗笑声与江湖气息。"
  }' --output out.wav
```

模型是 vd 时，`voice` 字段也可以直接当描述用。

**音色克隆**（传参考音频）

```bash
curl http://localhost:8081/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{
    "model": "mimo-v2.5-tts-voiceclone",
    "input": "你好",
    "reference_audio": "'$(base64 -w0 ref.mp3)'"
  }' --output out.wav
```

`reference_audio` 支持 base64（可带 `data:audio/...;base64,` 前缀）
或 http(s) URL。扩展名按 magic bytes 自动判断，
对象名固定为 `multimedia/{userId}/voice-clone.{ext}`。

★ 注意：`audioUrl` / `audio.voice` 必须填 **`resourceUrl`** 而不是
`uploadUrl` —— 两者 `Expires` 不同，填错返回 500（ASR 时已踩过一次）。

### 其他改动

- 上传逻辑抽成 `upload_audio()`，ASR 与音色克隆共用
- TTS 轮询改指数退避（0.6s 起步、×1.4、上限 2s，`tts.max_wait` 控制总时长）
- 非 JSON 响应给出前 120 字符诊断，不再只报"提交失败"
- 缺参数明确报错（克隆缺音频 / 设计缺描述），不会静默走错模式

回归：`python test_tts_vc_vd.py`（4 组 30 项）

## TTS / ASR 通用 API（无需私有字段）

客户端只需 OpenAI 标准字段就能用全部能力。三种 TTS 模式靠
**`model` + `voice`** 两个字段区分，`voice` 的含义随 model 变：

| model | `voice` 字段填什么 |
|---|---|
| `mimo-v2.5-tts` | 预设音色名，如 `冰糖` / `Mia` |
| `mimo-v2.5-tts-voicedesign` | 音色描述文字 |
| `mimo-v2.5-tts-voiceclone` | 参考音频：base64 / data URI / http URL / 表单文件 |

### 示例

**标准合成**

```bash
curl http://localhost:8081/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-tts","input":"你好","voice":"冰糖",
       "instructions":"温柔缓慢地读"}' --output out.wav
```

`instructions` 是 OpenAI 标准字段，映射到语气/风格描述。

**音色设计**

```bash
-d '{"model":"mimo-v2.5-tts-voicedesign","input":"哈哈哈！说得好！",
     "voice":"中年男性，武侠小说侠客风，声音洪亮豪爽。"}'
```

**音色克隆（JSON base64）**

```bash
-d '{"model":"mimo-v2.5-tts-voiceclone","input":"你好",
     "reference_audio":"'$(base64 -w0 ref.mp3)'"}'
```

**音色克隆（multipart 表单，OpenAI 惯例）**

```bash
curl http://localhost:8081/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" \
  -F model=mimo-v2.5-tts-voiceclone \
  -F input=你好 \
  -F "voice=@ref.mp3" \
  --output out.wav
```

### ASR 也支持表单上传

```bash
curl http://localhost:8081/v1/audio/transcriptions \
  -H "Authorization: Bearer sk-mimo" \
  -F model=mimo-v2.5-asr -F "file=@a.wav"
```

JSON 的 `file`(base64/URL) 方式继续可用。

### 音色清单

```
GET /v1/audio/voices
```

返回 8 个预设音色（含官方试听 URL）与三种模式的 voice 字段说明。

### 其他标准字段

| 字段 | 行为 |
|---|---|
| `response_format` | wav/mp3/flac/opus/aac/ogg/m4a。上游只产 wav，
                      需要其他格式时用 ffmpeg 转；**没有 ffmpeg 就如实
                      返回 wav**，绝不声明 mp3 却给 wav 数据 |
| `speed` | 接受但上游不支持，记日志后忽略 |
| `input` / `text` | 都认 |

### 响应头

```
X-Mimo-Tts-Mode:   实际使用的模型
X-Audio-Format:    实际返回的格式
```

### 兼容性

旧私有字段 `reference_audio` / `voice_desc` / `style` 继续可用，
不会被破坏。

回归：`python test_speech_api.py`（4 组 25 项）


## 音频流式输出

TTS 支持流式：`stream: true` 即用 chunked 推送，客户端边收边播。

```bash
curl -N http://localhost:8081/v1/audio/speech \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-tts","input":"很长的一段文本……",
       "voice":"冰糖","stream":true}' --output out.wav
```

（curl 要加 `-N` 禁用缓冲，否则看起来仍是"一次性到达"。）

### 两层流式

上游是"提交任务 → 轮询 → 拿 URL → 下载"的异步模型，本身不流式。
这里做了两层：

**1. 下载流式** —— 拿到音频 URL 后边下边转发，不用等整个文件落盘。

**2. 分段流式** —— 长文本按标点切成多段，每段独立合成，
**合成完一段立刻推送**，并可并发预取后续段。

实测 5 段文本：串行需 1.25s，并发预取 **0.79s**。更重要的是首段
延迟——原来要等全文合成完才有声音，现在第一段好了就开始播。

### wav 拼接问题

wav 是容器格式，多段首尾相接会出现 N 个 RIFF 头，播放器只认第一个，
结果是只播第一段。处理办法：

- **wav**（默认）：剥掉每段头只留 PCM，开头写一个"长度未知"的流式头
  （RIFF size 与 data size 都填 `0xFFFFFFFF`），Chrome / ffplay
  等主流解码器都接受
- **mp3**：帧格式可直接拼接，有 ffmpeg 时优先

### 配置项

```jsonc
"tts": {
  "stream_max_chars": 180,   // 超过这个长度才分段
  "stream_prefetch": 2       // 并发预取段数（越大越快，风控风险也越高）
}
```

### 响应头

```
Transfer-Encoding:     chunked
X-Mimo-Tts-Segments:   实际分段数
X-Audio-Format:        实际格式
```

### 兼容性

`stream` 不填或 `false` 走原来的非流式路径（带 Content-Length），
已有调用不受影响。客户端中途断开不算账号故障，不会标记失效。

回归：`python test_audio_stream.py`（6 组 30 项）

## 新增端点：docx / tools

### docx（纯标准库，无需 python-docx）

```bash
# 生成 Word 文档
curl -X POST http://HOST:8081/v1/docx/generate \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{"markdown":"# 标题\n\n正文","title":"报告","filename":"报告"}' --output out.docx

# 解析 docx 提取文本
curl -X POST http://HOST:8081/v1/docx/parse \
  -H "Authorization: Bearer sk-mimo" \
  --data-binary @out.docx
```

字段：`markdown` / `input` / `text` 任一，`title` 可选，`filename` 可选。
支持 `# 标题`、`- 列表`、`> 引用`；解析返回 `{"text","paragraphs","chars"}`。

### tools（工具清单与直接执行）

```bash
curl http://HOST:8081/v1/tools -H "Authorization: Bearer sk-mimo"
curl -X POST http://HOST:8081/v1/tools/call \
  -H "Authorization: Bearer sk-mimo" -H "Content-Type: application/json" \
  -d '{"name":"claw_container_status","arguments":{}}'
```

仅 claw 内置工具可被代理直接执行（需 `claw.expose_tools: true`）。
客户端自带的 tools 需自行实现并回传结果，走标准 tool 消息即可。
