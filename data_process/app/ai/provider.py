# -*- coding: utf-8 -*-
"""LLM 提供方热切换（在线 API / 本地模型）。

设计：
  * 模式二元语义：online = DeepSeek 在线 API（配置来自 .env）；
    local = 本地 Ollama（配置为内置默认值，与 .env 曾经的 Ollama 块一致）；
  * 切换时重建 LLMClient 并重绑定所有持有者：
      - /api/v1/ai/*  的 AIService 单例
      - /api/v1/assistant/* 的 MedicalAssistantService（含其内部 AIService）
    持有者通过各自 rebind_client(client) 同步更新 _client/_generator/_agent，
    保证「在线/本地」热切换后所有 AI 路径（Agent 规划、摘要生成、grounded 兜底）
    立即生效，无需重启进程；
  * 在线配置每次从 Settings.load()（.env / 环境变量）读取，不受进程内
    切换覆盖影响，可反复往返切换。
"""

from __future__ import annotations

import threading
from typing import Any

from config.settings import Settings

# 模式标识（与前端开关一一对应）
ONLINE_MODE = "online"      # DeepSeek 在线 API
LOCAL_MODE = "local"        # 本地 Ollama

# 本地模型默认配置（Ollama 原生端点，think=false 关闭思考链）
LOCAL_LLM_CONFIG: dict[str, Any] = {
    "llm_provider": "ollama",
    "llm_api_key": "ollama",
    "llm_base_url": "http://localhost:11434",
    "llm_model": "qwen3.5:4b",
    "llm_timeout": 60,
    "llm_max_retries": 1,
    "llm_retry_budget_seconds": 90.0,
    "llm_ollama_think": False,
    "llm_temperature": 0.2,
    "llm_max_tokens": 4000,
    "llm_extra_body": "",
}

_lock = threading.Lock()


def _apply(settings: Settings, cfg: dict[str, Any]) -> None:
    """把配置写入 settings（供 describe_client / meta 展示与 build_client 使用）。"""
    for key, value in cfg.items():
        setattr(settings, key, value)


def get_current_mode(settings: Settings) -> str:
    """按当前 provider 推断模式：ollama -> local，其余（deepseek/openai/auto 等）-> online。"""
    provider = str(getattr(settings, "llm_provider", "") or "").strip().lower()
    return LOCAL_MODE if provider == "ollama" else ONLINE_MODE


def _online_config() -> dict[str, Any]:
    """从 .env / 环境变量读取在线（DeepSeek）配置，避免受进程内切换覆盖影响。"""
    fresh = Settings.load()
    return {
        "llm_provider": fresh.llm_provider or "auto",
        "llm_api_key": fresh.llm_api_key or "",
        "llm_base_url": fresh.llm_base_url or "",
        "llm_model": fresh.llm_model or "deepseek-chat",
        "llm_timeout": fresh.llm_timeout,
        "llm_max_retries": fresh.llm_max_retries,
        "llm_retry_budget_seconds": fresh.llm_retry_budget_seconds,
        "llm_ollama_think": bool(getattr(fresh, "llm_ollama_think", False)),
        "llm_temperature": fresh.llm_temperature,
        "llm_max_tokens": fresh.llm_max_tokens,
        "llm_extra_body": str(getattr(fresh, "llm_extra_body", "") or ""),
    }


def switch_llm_mode(mode: str, settings: Settings) -> dict:
    """切换到指定模式并重绑定全部 LLM 持有者，返回 describe_client 摘要。

    mode: online（DeepSeek API） / local（本地 Ollama）。
    """
    mode = (mode or "").strip().lower()
    if mode not in (ONLINE_MODE, LOCAL_MODE):
        raise ValueError(f"不支持的 LLM 模式: {mode}（可选 online / local）")

    with _lock:
        if mode == LOCAL_MODE:
            _apply(settings, LOCAL_LLM_CONFIG)
        else:
            _apply(settings, _online_config())

        from app.ai.summary.llm_client import build_client, describe_client

        client = build_client(settings)
        _rebind_all_clients(client)
        return describe_client(client, settings)


def _rebind_all_clients(client) -> None:
    """重建 client 后，更新所有持有 LLM 客户端的运行时实例。"""
    from app.extensions import ext

    # 1) /api/v1/ai/* 的 AIService 单例（首次访问时创建，与 ai.py _service() 行为一致）
    from app.ai.service import get_ai_service

    ai_svc = get_ai_service(ext.settings)
    ai_svc.rebind_client(client)

    # 2) /api/v1/assistant/* 的 MedicalAssistantService（内部含独立 AIService）
    assistant = getattr(ext, "application_service", None)
    if assistant is not None:
        assistant.rebind_client(client)
