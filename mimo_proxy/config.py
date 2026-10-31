# -*- coding: utf-8 -*-
"""
配置模块
============================================================
所有设置集中在 config.json，不再依赖环境变量。

优先级（后者覆盖前者）:
    内置默认值  <  config.json  <  环境变量覆盖（可选，见 env_override）

配置文件查找顺序:
    1. --config 命令行参数指定
    2. 环境变量 MIMO_CONFIG 指定
    3. 当前目录 ./config.json
    4. 包目录 ./mimo_proxy/config.json
    5. 找不到则自动生成一份默认配置（不覆盖已有）
"""

import os
import sys
import json
import copy

# ---------------------------------------------------------------- 默认值

DEFAULTS = {
    "server": {
        "host": "0.0.0.0",
        "port": 8081,
    },

    # 调用方使用的 API Key（OpenAI / Anthropic 客户端填这个）
    "auth": {
        "api_key": "sk-mimo",
    },

    # 管理面板登录（独立于小米账号）
    "admin": {
        "user": "admin",
        "pass": "admin",
        # 多管理员；非空时优先于上面的 user/pass
        # 例: [{"user": "ops", "pass": "xxx"}]
        "accounts": [],
        "session_ttl": 43200,          # 会话有效期，秒（12 小时）
    },

    # 小米账号池
    "accounts": {
        # 账号持久化文件（敏感字段加密）
        "file": "accounts.json",
        # token 失效时是否用账号密码自动重登
        "auto_refresh": True,
        # 账号列表，每项二选一：
        #   {"user": "小米邮箱/手机号", "pass": "密码"}
        #   {"ph": "xxx", "token": "yyy", "uid": "zzz"}
        "list": [],
        # 单账号快捷写法（list 为空时生效）
        "ph": "",
        "token": "",
        "uid": "",
    },

    # 上游 MiMo
    "mimo": {
        "base": "https://aistudio.xiaomimimo.com",
        "model_map": {
            "mimo-v2.5-pro": "mimo-v2.6-pro",
            "mimo-v2.5": "mimo-v2.6-flash",
            "mimo-v2.6-pro": "mimo-v2.6-pro",
            "mimo-v2.6-flash": "mimo-v2.6-flash",
        },
        "timeout": 120,
    },

    # TTS
    "tts": {
        "voice": "冰糖",
        "scene": "BRIEF_DESCRIPTION",
        "format": "wav",
        # OpenAI 音色名 -> MiMo 中文音色
        "voice_map": {
            "alloy": "冰糖", "echo": "冰糖", "fable": "冰糖",
            "onyx": "冰糖", "nova": "冰糖", "shimmer": "冰糖",
        },
        # 已入库会话的缓存时长（秒）
        "conversation_ttl": 1800,
    },

    # ASR
    "asr": {
        "model": "mimo-v2.5-asr",
        "language": "auto",
        "max_bytes": 10485760,        # 10MB
    },

    # mimo-claw 沙箱
    "claw": {
        # 沙箱文件端点前缀（早期抓包只记录了名称，未完整实证，可调）
        "host_files_prefix": "/open-apis/user/mimo-claw/host-files",
        "release_path": "/open-apis/user/mimo-claw/release",
        "auto_create_on_start": False,   # 启动时为无容器的账号自动申请
        "create_wait_timeout": 90,       # 等待容器就绪的秒数
        "ensure_cache_ttl": 120,         # 容器状态缓存秒数，0=不缓存
        # 把沙箱能力包装成 OpenAI 工具注入给模型
        "expose_tools": False,
        # 代理自己执行 claw 工具并回灌（客户端无需实现）
        "auto_execute": False,
        "auto_execute_rounds": 3,        # 自动执行最多几轮
        # ---- 定时调度：一个号一个建、每号每天限次、每 N 小时一轮 ----
        "auto_schedule": False,          # 开启后台调度
        "schedule_interval_hours": 3,    # 每几小时跑一轮
        "schedule_on_start": False,      # 启动时立即跑一轮
        "daily_limit_per_account": 1,    # 每个账号每天最多申请几次
        "schedule_account_gap": 3,       # 账号之间的间隔秒数（串行）
        "only_one_container": True,      # 只要有一个号有容器就够，不再给别的号建
        "state_file": "claw_state.json", # 调度状态落盘（重启后仍记得）
    },

    # 其他
    "misc": {
        "max_logs": 200,
        "log_requests": True,
    },
}

# 环境变量 -> 配置路径 的映射（可选兼容层）
# 设置 ALLOW_ENV_OVERRIDE=false 可完全关闭，强制只用 config.json
ENV_MAP = {
    "PORT":                ("server", "port", int),
    "API_KEY":             ("auth", "api_key", str),
    "AUTO_REFRESH":        ("accounts", "auto_refresh", lambda v: str(v).lower() == "true"),
    "ADMIN_USER":          ("admin", "user", str),
    "ADMIN_PASS":          ("admin", "pass", str),
    "ADMIN_SESSION_TTL":   ("admin", "session_ttl", int),
    "ACCOUNTS_FILE":       ("accounts", "file", str),
    "MIMO_PH":             ("accounts", "ph", str),
    "MIMO_TOKEN":          ("accounts", "token", str),
    "MIMO_UID":            ("accounts", "uid", str),
    "MIMO_TTS_VOICE":      ("tts", "voice", str),
    "MIMO_TTS_SCENE":      ("tts", "scene", str),
    "MIMO_TTS_FORMAT":     ("tts", "format", str),
}

# 允许多值 JSON 的环境变量
ENV_JSON_MAP = {
    "MIMO_ACCOUNTS":   ("accounts", "list"),
    "ADMIN_ACCOUNTS":  ("admin", "accounts"),
}


def _deep_merge(base, override):
    """递归合并，override 优先"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_path(cfg, path, value):
    node = cfg
    for p in path[:-1]:
        node = node.setdefault(p, {})
    node[path[-1]] = value


def _candidate_paths():
    """配置文件查找顺序"""
    cands = []
    # 1. --config 参数
    for i, a in enumerate(sys.argv):
        if a == "--config" and i + 1 < len(sys.argv):
            cands.append(sys.argv[i + 1])
        elif a.startswith("--config="):
            cands.append(a.split("=", 1)[1])
    # 2. 环境变量
    if os.environ.get("MIMO_CONFIG"):
        cands.append(os.environ["MIMO_CONFIG"])
    # 3. 当前目录
    cands.append(os.path.join(os.getcwd(), "config.json"))
    # 4. 包目录
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    return cands


def load_config(path=None):
    """加载配置，返回 dict。找不到配置文件时创建默认文件。"""
    cfg = copy.deepcopy(DEFAULTS)

    loaded_from = None
    if path:
        paths = [path]
    else:
        paths = _candidate_paths()

    for p in paths:
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = _deep_merge(cfg, json.load(f))
                loaded_from = p
                break
            except Exception as e:
                sys.stderr.write(f"[warn] 读取配置 {p} 失败: {e}\n")

    # 环境变量覆盖（兼容旧用法）
    if os.environ.get("ALLOW_ENV_OVERRIDE", "true").lower() != "false":
        for env, (sec, key, caster) in ENV_MAP.items():
            if env in os.environ:
                try:
                    _set_path(cfg, (sec, key), caster(os.environ[env]))
                except Exception as e:
                    sys.stderr.write(f"[warn] 环境变量 {env} 解析失败: {e}\n")
        for env, (sec, key) in ENV_JSON_MAP.items():
            if env in os.environ:
                try:
                    _set_path(cfg, (sec, key), json.loads(os.environ[env]))
                except Exception as e:
                    sys.stderr.write(f"[warn] 环境变量 {env} JSON 解析失败: {e}\n")

    cfg["_loaded_from"] = loaded_from
    return cfg


def save_default(path):
    """写一份默认配置文件（不覆盖已存在的）"""
    if os.path.exists(path):
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
    return True


# ---------------------------------------------------------------- 全局配置对象

class Config:
    """配置访问封装：属性式读取，避免各模块散落 os.environ"""

    def __init__(self, data=None):
        self._d = data if data is not None else load_config()

    @property
    def data(self):
        return self._d

    def get(self, section, key=None, default=None):
        if key is None:
            return self._d.get(section, default)
        return self._d.get(section, {}).get(key, default)

    def reload(self, path=None):
        self._d = load_config(path)
        return self._d

    def save(self, path):
        d = {k: v for k, v in self._d.items() if not k.startswith("_")}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    # ---- 常用快捷属性 ----
    @property
    def port(self):
        return int(self.get("server", "port", 8081))

    @property
    def host(self):
        return self.get("server", "host", "0.0.0.0")

    @property
    def api_key(self):
        return self.get("auth", "api_key", "sk-mimo")

    @property
    def auto_refresh(self):
        return bool(self.get("accounts", "auto_refresh", True))

    @property
    def accounts_file(self):
        return self.get("accounts", "file", "accounts.json")

    @property
    def mimo_base(self):
        return self.get("mimo", "base", "https://aistudio.xiaomimimo.com")

    @property
    def model_map(self):
        return self.get("mimo", "model_map", {}) or {}

    @property
    def timeout(self):
        return int(self.get("mimo", "timeout", 120))


# 全局单例
CFG = Config()
