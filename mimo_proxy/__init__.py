# -*- coding: utf-8 -*-
"""
MiMo v2.6 轻量代理（模块化版）
============================================================
把 OpenAI / Anthropic / Responses 请求翻译为小米 MiMo 网页端协议。

模块结构:
    config.py    配置（config.json）
    utils.py     日志 / 统计 / 加密 / ID
    accounts.py  账号与账号池
    mimo.py      上游 HTTP 客户端
    tools.py     工具调用注入与提取
    speech.py    TTS / ASR
    convert.py   Anthropic / Responses 转换
    admin.py     管理面板鉴权
    panel.py     管理面板 HTML
    server.py    HTTP 服务与启动

启动:
    python -m mimo_proxy
    python -m mimo_proxy --config /path/config.json
"""

__version__ = "2.0.0"

from .config import CFG, load_config, save_default, DEFAULTS

__all__ = ["CFG", "load_config", "save_default", "DEFAULTS", "__version__"]
