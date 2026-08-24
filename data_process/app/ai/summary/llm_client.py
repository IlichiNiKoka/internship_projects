# -*- coding: utf-8 -*-
"""LLM 客户端（需求 3.X.2）。

设计：
  1. 抽象 `LLMClient` 接口，单一方法 `chat(system, user) -> str`；
  2. `OpenAICompatibleClient`：用 openai SDK，兼容 OpenAI / DeepSeek / 本地 vLLM；
  3. `MockClient`：不调网络，按模板渲染（开发与测试默认走这里）；
  4. `DisabledClient`：完全禁用，返回固定提示；
  5. `build_client(settings)`：按配置自动选择，API key 留空 -> Mock。

DeepSeek 接入：
  - base_url=https://api.deepseek.com/v1
  - 默认 model=deepseek-chat（V3），可改为 deepseek-reasoner（R1）
  - 协议与 OpenAI 完全兼容，复用同一 SDK 调用代码

TODO (Phase 2 - LLM 本地化部署):
  1. 新增 LocalLLMClient：支持 Qwen/BaiChuan 等本地模型离线推理
  2. 支持 llama.cpp / Ollama / vLLM 本地服务模式（OpenAI 兼容 API）
  3. 模型量化配置：4bit/8bit 量化降低显存占用
  4. 模型热加载/卸载：按需加载不同规模模型
  5. 配置扩展：llm_local_model_path, llm_gpu_layers, llm_context_size 等
  6. build_client 自动检测本地模型可用性（provider=local）
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# DeepSeek 默认 base_url（DeepSeek API 与 OpenAI 协议兼容）
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


# ---------------------------------------------------------------------------
# 接口契约
# ---------------------------------------------------------------------------
class LLMClient(Protocol):
    """LLM 客户端协议。"""
    provider: str  # 标识符，便于上层展示与日志

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        """单轮对话：返回生成的文本。"""
        ...


# ---------------------------------------------------------------------------
# OpenAI 兼容客户端（DeepSeek / OpenAI / 本地 vLLM 通用）
# ---------------------------------------------------------------------------
class OpenAICompatibleClient:
    """通过 openai SDK 调用任意 OpenAI 兼容端点。

    适用于：
      - OpenAI 官方（默认 base_url，model=gpt-4o-mini 等）
      - DeepSeek（base_url=https://api.deepseek.com/v1，model=deepseek-chat）
      - 本地 LLM（vLLM / Ollama OpenAI 兼容，base_url=http://localhost:8000/v1）
    """

    def __init__(self, api_key: str, model: str, base_url: str = "",
                 timeout: int = 30, temperature: float = 0.2,
                 max_tokens: int = 800, provider: str = "openai"):
        # 延迟导入：仅在真正需要调用 LLM 时才 import openai
        try:
            from openai import OpenAI  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "未安装 openai SDK，请运行：pip install openai>=1.30"
            ) from e

        self._api_key = api_key
        self._model = model
        self._base_url = base_url or None  # None -> openai SDK 走官方
        self._timeout = timeout
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.provider = provider
        self._OpenAI_cls = OpenAI

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        """调用 chat completions API。"""
        client = self._OpenAI_cls(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
        )
        try:
            resp = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            text = resp.choices[0].message.content or ""
            logger.debug("LLM 调用成功 provider=%s model=%s len=%d",
                        self.provider, self._model, len(text))
            return text.strip()
        except Exception as e:
            logger.warning("LLM 调用失败 provider=%s model=%s err=%s",
                          self.provider, self._model, e)
            # 调用失败 -> 走 mock 兜底，保证 API 可用
            return ""


# ---------------------------------------------------------------------------
# Mock 客户端：API key 留空时的默认降级方案
# ---------------------------------------------------------------------------
class MockClient:
    """不调网络的 Mock 客户端：返回标记字符串，由上层生成器用模板渲染。"""

    provider = "mock"

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        logger.debug("Mock LLM 调用（不实际请求网络）")
        # 返回特殊标记，由 generator 识别并改走模板渲染
        return "__MOCK__:不调用实际 LLM，使用本地模板渲染"


# ---------------------------------------------------------------------------
# 禁用客户端
# ---------------------------------------------------------------------------
class DisabledClient:
    """LLM 完全禁用：返回固定提示。"""

    provider = "disabled"

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        return "LLM 文本生成已被禁用，请配置 ANALYTICS_LLM_PROVIDER 后重试。"


# ---------------------------------------------------------------------------
# 工厂入口
# ---------------------------------------------------------------------------
def build_client(settings: Any) -> LLMClient:
    """按 settings 配置自动选择客户端。

    选择规则：
      1. provider=disabled -> DisabledClient
      2. provider=mock -> MockClient
      3. provider=auto：有 api_key 用 OpenAI 兼容客户端；无 api_key 用 MockClient
      4. provider=deepseek：强制走 DeepSeek base_url
      5. provider=openai：走 OpenAI 官方（base_url 留空）
    """
    provider = (settings.llm_provider or "auto").lower()
    api_key = settings.llm_api_key or ""

    if provider == "disabled":
        return DisabledClient()
    if provider == "mock":
        return MockClient()
    if provider == "auto":
        if not api_key:
            logger.info("LLM provider=auto 且 API key 留空 -> 自动降级到 MockClient")
            return MockClient()
        # 有 key：按 model 名推断 provider
        if settings.llm_model.startswith("deepseek"):
            provider = "deepseek"
        else:
            provider = "openai"

    if provider == "deepseek":
        base_url = settings.llm_base_url or DEEPSEEK_BASE_URL
        client_provider = "deepseek"
    elif provider == "openai":
        base_url = settings.llm_base_url  # 留空 -> openai SDK 走官方
        client_provider = "openai"
    else:
        # 未知 provider 字符串 -> 当作自定义 OpenAI 兼容端点
        base_url = settings.llm_base_url
        client_provider = provider

    if not api_key:
        logger.warning("provider=%s 但 API key 留空 -> 降级到 MockClient", provider)
        return MockClient()

    return OpenAICompatibleClient(
        api_key=api_key,
        model=settings.llm_model,
        base_url=base_url,
        timeout=settings.llm_timeout,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        provider=client_provider,
    )


def describe_client(client: LLMClient, settings: Any) -> dict:
    """对外暴露当前 LLM 配置摘要（不泄露 api_key）。"""
    return {
        "provider": getattr(client, "provider", "unknown"),
        "model": settings.llm_model,
        "base_url": (settings.llm_base_url or
                     ("https://api.deepseek.com/v1"
                      if getattr(client, "provider", "") == "deepseek"
                      else "")),
        "api_key_configured": bool(settings.llm_api_key),
    }
