# -*- coding: utf-8 -*-
"""LLM 客户端（需求 3.X.2 + 3.X.5 LLM 本地化部署）。

设计：
  1. 抽象 `LLMClient` 接口，单一方法 `chat(system, user) -> str`；
  2. `OpenAICompatibleClient`：用 openai SDK，兼容 OpenAI / DeepSeek / 本地 vLLM / Ollama；
  3. `MockClient`：不调网络，按模板渲染（开发与测试默认走这里）；
  4. `DisabledClient`：完全禁用，返回固定提示；
  5. `build_client(settings)`：按配置自动选择，API key 留空 -> Mock。

DeepSeek 接入：
  - base_url=https://api.deepseek.com/v1
  - 默认 model=deepseek-chat（V3），可改为 deepseek-reasoner（R1）
  - 协议与 OpenAI 完全兼容，复用同一 SDK 调用代码

Ollama 本地部署：
  - base_url=http://localhost:11434/v1 (Ollama OpenAI 兼容端点)
  - model=qwen3.5:4b (或其他已 pull 的模型)
  - 无需 API key (可任意填写如 "ollama")
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# DeepSeek 默认 base_url（DeepSeek API 与 OpenAI 协议兼容）
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
# Ollama 默认 base_url (Ollama OpenAI 兼容端点)
OLLAMA_BASE_URL = "http://localhost:11434/v1"


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
                 max_tokens: int = 800, provider: str = "openai",
                 max_retries: int = 1, retry_budget_seconds: float = 45.0,
                 extra_body: dict | None = None):
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
        # 重试策略：max_retries 次重试 + 总预算双重限制。
        # 超出预算立即放弃并返回空串（上层走 Mock 兜底），
        # 避免单次慢调用把用户请求阻塞几十秒。
        self._max_retries = max(0, int(max_retries))
        self._retry_budget = float(retry_budget_seconds)
        # 额外请求体透传（如 {"reasoning_effort": "none"} 关闭远程推理模型思考链）
        self._extra_body = extra_body or None
        self._OpenAI_cls = OpenAI
        # 懒加载单例客户端：OpenAI 对象 + httpx.Client 是连接池，
        # 每次调用都重建会重复 TLS 握手与建连（数百 ms 级开销）。
        self._client = None

    def _get_client(self):
        """懒加载并复用 OpenAI/httpx 客户端（线程安全：httpx.Client 支持并发复用）。"""
        if self._client is None:
            import httpx
            # trust_env=False：忽略系统/环境代理（如 Windows 注册表代理）。
            # 否则本机代理未运行时，连 localhost Ollama 也会被代理拦截报 Connection error。
            self._client = self._OpenAI_cls(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                http_client=httpx.Client(trust_env=False, timeout=self._timeout),
            )
        return self._client

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        """调用 chat completions API（空响应自动重试）。"""
        client = self._get_client()
        import time
        started = time.perf_counter()
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            # 总预算检查：剩余预算不足以再发起一次完整调用时直接放弃，
            # 避免「每次都等到超时、重试把阻塞时间成倍放大」的体验。
            elapsed = time.perf_counter() - started
            if attempt > 0 and (elapsed + self._timeout) > self._retry_budget:
                logger.warning("LLM 重试预算耗尽 provider=%s model=%s 已耗时=%.1fs 预算=%.1fs",
                               self.provider, self._model, elapsed, self._retry_budget)
                break
            try:
                resp = client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    **({"extra_body": self._extra_body} if self._extra_body else {}),
                )
                # 健壮解析：部分推理模型（如 nemotron / deepseek-reasoner）
                # 可能返回空 choices 或 content=None（正文在 reasoning 字段）
                choices = getattr(resp, "choices", None) or []
                msg = getattr(choices[0], "message", None) if choices else None
                text = (getattr(msg, "content", None) or "")
                if not text.strip():
                    # 兜底：取推理字段内容，避免整次调用白费
                    text = (getattr(msg, "reasoning_content", None)
                            or getattr(msg, "reasoning", None) or "")
                text = text.strip()
                if text:
                    logger.debug("LLM 调用成功 provider=%s model=%s len=%d attempt=%d",
                                self.provider, self._model, len(text), attempt)
                    return text
                # 空响应：免费端点偶发限流，短暂等待后重试
                logger.warning("LLM 空响应 provider=%s model=%s attempt=%d",
                              self.provider, self._model, attempt)
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
            except Exception as e:
                last_err = e
                logger.warning("LLM 调用失败 provider=%s model=%s attempt=%d err=%s",
                              self.provider, self._model, attempt, e)
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        if last_err is not None:
            logger.warning("LLM 重试耗尽 provider=%s model=%s err=%s",
                          self.provider, self._model, last_err)
        # 全部失败 -> 返回空串，走 mock 兜底，保证 API 可用
        return ""


class OllamaNativeClient:
    """Ollama 原生 /api/chat 客户端（provider="ollama" 时启用）。

    为什么不走 OpenAI 兼容端点：
      * 兼容端点不支持 `think` 参数，qwen3.5 这类思考模型会先花大量时间/token
        在思考链上（实测摘要生成 ~26s），且正文可能为空、内容全在 reasoning；
      * 原生 API 可传 think=false 关闭思考（实测同请求 <2s），延迟降低一个量级。
    重试/预算策略与 OpenAICompatibleClient 一致。
    """

    provider = "ollama"

    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 timeout: int = 60, temperature: float = 0.2,
                 max_tokens: int = 4000, think: bool = False,
                 max_retries: int = 1, retry_budget_seconds: float = 45.0):
        try:
            import httpx  # noqa: F401 —— openai SDK 已依赖 httpx，此处仅确认可用
        except ImportError as e:
            raise RuntimeError("未安装 httpx，请运行：pip install httpx") from e
        self._model = model
        self._base_url = (base_url or "http://localhost:11434").rstrip("/")
        self._timeout = int(timeout)
        self._temperature = float(temperature)
        self._max_tokens = int(max_tokens)
        self._think = bool(think)
        self._max_retries = max(0, int(max_retries))
        self._retry_budget = float(retry_budget_seconds)

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        import time
        import httpx

        url = f"{self._base_url}/api/chat"
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "think": self._think,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._max_tokens,
            },
        }
        started = time.perf_counter()
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            elapsed = time.perf_counter() - started
            if attempt > 0 and (elapsed + self._timeout) > self._retry_budget:
                logger.warning("LLM(Ollama) 重试预算耗尽 model=%s 已耗时=%.1fs 预算=%.1fs",
                               self._model, elapsed, self._retry_budget)
                break
            try:
                # trust_env=False：忽略系统代理，避免 localhost 被代理拦截
                resp = httpx.post(url, json=payload, timeout=self._timeout,
                                  trust_env=False)
                resp.raise_for_status()
                data = resp.json()
                msg = data.get("message") or {}
                text = (msg.get("content") or msg.get("thinking") or "").strip()
                if text:
                    logger.debug("LLM(Ollama) 成功 model=%s len=%d attempt=%d",
                                 self._model, len(text), attempt)
                    return text
                logger.warning("LLM(Ollama) 空响应 model=%s attempt=%d",
                               self._model, attempt)
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("LLM(Ollama) 调用失败 model=%s attempt=%d err=%s",
                               self._model, attempt, e)
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        if last_err is not None:
            logger.warning("LLM(Ollama) 重试耗尽 model=%s err=%s", self._model, last_err)
        # 全部失败 -> 返回空串，走 mock 兜底，保证 API 可用
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
def _parse_extra_body(raw: str | None) -> dict | None:
    """把 ANALYTICS_LLM_EXTRA_BODY（JSON 字符串）解析为 dict，非法值忽略并告警。"""
    if not raw or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        logger.warning("ANALYTICS_LLM_EXTRA_BODY 不是合法 JSON，已忽略: %r", raw)
        return None


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

    if provider == "ollama":
        # Ollama 本地部署：走原生 /api/chat（可传 think=false 关闭思考链，
        # qwen3.5 等思考模型延迟从 ~26s 降到 <2s）
        return OllamaNativeClient(
            model=settings.llm_model,
            base_url=(settings.llm_base_url or OLLAMA_BASE_URL).replace("/v1", ""),
            timeout=settings.llm_timeout,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            think=bool(getattr(settings, "llm_ollama_think", False)),
            max_retries=int(getattr(settings, "llm_max_retries", 1)),
            retry_budget_seconds=float(getattr(settings, "llm_retry_budget_seconds", 45.0)),
        )

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
        max_retries=int(getattr(settings, "llm_max_retries", 1)),
        retry_budget_seconds=float(getattr(settings, "llm_retry_budget_seconds", 45.0)),
        extra_body=_parse_extra_body(getattr(settings, "llm_extra_body", "")),
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
