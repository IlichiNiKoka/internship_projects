# -*- coding: utf-8 -*-
"""LLM 增强意图分类器（Phase 2 - TODO #2/#3/#5 实现）。

设计原则：本地只做预处理与 prompt 编排，意图理解交给 LLM。

策略：
  1. 规则引擎做快速路径（已有 IntentClassifier），多数查询 0 延迟命中；
  2. 规则引擎返回 unsupported 或置信度低于阈值时，回退到 LLM 分类；
  3. LLM 接收结构化 prompt（意图目录 + 可用维度/指标），输出 JSON；
  4. 本地解析 JSON -> IntentResult，保证与规则引擎相同契约；
  5. 支持 classify_multi()：单次查询可能包含多个意图，LLM 识别后
     本地逐一编排下游调用；
  6. MockClient / DisabledClient 时自动降级到规则引擎结果，不阻塞流程。

能力覆盖（对应 classifier.py TODO）：
  * #2 模糊查询：LLM 天然处理拼写错误、口语表达、同义改写；
  * #3 多意图识别：LLM 输出 intent 数组，本地串行编排；
  * #5 闲聊/非业务查询识别：LLM 在 prompt 中被指示返回 {"intent":"unsupported"}。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.ai.intent.catalog import INTENT_BY_KEY, INTENTS
from app.ai.intent.classifier import IntentClassifier, IntentResult
from app.ai.intent.terms import DIMENSION_KEYWORDS, METRIC_KEYWORDS
from app.ai.summary.llm_client import LLMClient, MockClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt 构造（本地预处理 + prompt steering）
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
你是一个医疗大数据查询意图分类器。

你的任务：把用户的自然语言查询分类为以下意图之一（或多个），并抽取参数。

【可用意图】
{intent_catalog}

【可用维度（分组字段）】
{dimensions}

【可用指标（聚合度量）】
{metrics}

【输出格式】
返回 JSON 数组，每个元素代表一个识别到的意图：
```json
[
  {{
    "intent": "意图key",
    "confidence": 0.0到1.0的置信度,
    "params": {{}},
    "reason": "简短理由"
  }}
]
```

【规则】
1. intent 必须是上面列出的意图 key 之一；
2. 如果查询不属于医疗数据分析范围（闲聊、天气、新闻等），返回 [{{"intent":"unsupported","confidence":0.9,"params":{{}}}}]；
3. 一个查询可能包含多个意图（如"统计总览并预测费用"），全部识别出来；
4. aggregation_query 的 params 需含 dimensions 数组（从可用维度中选）；
5. 过滤条件放入 params.filters 数组，每项 {{"field":"维度key","op":"eq","value":"取值"}}；
6. 年份直接作为数字放入 filter value；
7. 只返回 JSON，不要额外解释。
"""

_USER_PROMPT_TEMPLATE = """用户查询：{query}

请分类并抽取参数，返回 JSON。"""


def _build_intent_catalog() -> str:
    """把意图目录格式化为 prompt 片段。"""
    lines = []
    for spec in INTENTS:
        req = ", ".join(spec.requires_params) if spec.requires_params else "无"
        opt = ", ".join(spec.optional_params) if spec.optional_params else "无"
        lines.append(
            f"- {spec.key}（{spec.label_cn}）: {spec.description}\n"
            f"  必填参数: {req}; 可选参数: {opt}; 下游: {spec.downstream}"
        )
    return "\n".join(lines)


def _build_dimensions_text() -> str:
    return ", ".join(DIMENSION_KEYWORDS.keys())


def _build_metrics_text() -> str:
    return ", ".join(METRIC_KEYWORDS.keys())


_SYSTEM_PROMPT_RENDERED = _SYSTEM_PROMPT.format(
    intent_catalog=_build_intent_catalog(),
    dimensions=_build_dimensions_text(),
    metrics=_build_metrics_text(),
)


def _build_user_prompt(query: str) -> str:
    return _USER_PROMPT_TEMPLATE.format(query=query or "")


# ---------------------------------------------------------------------------
# LLM 响应解析（本地后处理）
# ---------------------------------------------------------------------------
_JSON_BLOCK_RE = re.compile(r"\[[\s\S]*\]")
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> list[dict] | None:
    """从 LLM 文本中提取 JSON 数组或单个对象，容错解析。"""
    if not text:
        return None
    # 优先找 JSON 数组
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass
    # 退而求其次找单个对象
    m = _OBJECT_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass
    return None


def _parse_llm_items(items: list[dict], query: str) -> list[IntentResult]:
    """把 LLM 返回的 JSON 项列表转为 IntentResult 列表。"""
    results: list[IntentResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent") or "unsupported").strip()
        if intent not in INTENT_BY_KEY:
            intent = "unsupported"
        confidence = float(item.get("confidence") or 0.5)
        confidence = max(0.0, min(1.0, confidence))
        params = item.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        spec = INTENT_BY_KEY[intent]
        missing = [p for p in spec.requires_params if p not in params]
        results.append(IntentResult(
            query=query,
            intent=intent,
            confidence=confidence,
            params=params,
            missing_required=missing,
            matched_signals={"llm": [str(item.get("reason") or "")]},
        ))
    return results


# ---------------------------------------------------------------------------
# 增强分类器
# ---------------------------------------------------------------------------
class LLMAugmentedClassifier:
    """规则引擎 + LLM 双层意图分类器。

    使用：
        classifier = LLMAugmentedClassifier(
            rule_classifier=IntentClassifier(),
            llm_client=build_client(settings),
            fallback_threshold=0.5,
        )
        result = classifier.classify("帮我看下老人住院情况大概花多少钱")
        # result 是单个 IntentResult（取最高置信度）

        results = classifier.classify_multi("总览统计一下，再预测费用")
        # results 是 IntentResult 列表
    """

    def __init__(
        self,
        *,
        rule_classifier: IntentClassifier,
        llm_client: LLMClient | None = None,
        fallback_threshold: float = 0.5,
    ):
        self._rule = rule_classifier
        self._llm = llm_client
        self._threshold = fallback_threshold

    def classify(self, query: str) -> IntentResult:
        """单意图分类：先规则引擎，不达标时回退 LLM 取最高置信度。"""
        rule_result = self._rule.classify(query)
        # 规则引擎高置信度命中 -> 直接返回
        if rule_result.intent != "unsupported" and rule_result.confidence >= self._threshold:
            return rule_result
        # LLM 不可用 -> 返回规则结果
        if self._llm is None or isinstance(self._llm, (MockClient,)):
            return rule_result
        # LLM 回退
        llm_results = self._llm_classify(query)
        if not llm_results:
            return rule_result
        # 取置信度最高的
        best = max(llm_results, key=lambda r: r.confidence)
        # 如果 LLM 也判 unsupported，保留规则引擎的信号供调试
        if best.intent == "unsupported" and rule_result.intent != "unsupported":
            return rule_result
        return best

    def classify_multi(self, query: str) -> list[IntentResult]:
        """多意图分类：LLM 识别全部意图，规则引擎结果作为补充。"""
        rule_result = self._rule.classify(query)
        if self._llm is None or isinstance(self._llm, (MockClient,)):
            return [rule_result]
        llm_results = self._llm_classify(query)
        if not llm_results:
            return [rule_result]
        # 如果 LLM 只识别到一个意图且与规则引擎一致，用规则引擎结果（参数更精确）
        if len(llm_results) == 1 and llm_results[0].intent == rule_result.intent:
            return [rule_result]
        return llm_results

    def _llm_classify(self, query: str) -> list[IntentResult]:
        """调用 LLM 做意图分类，失败时返回空列表。"""
        try:
            raw = self._llm.chat(_SYSTEM_PROMPT_RENDERED, _build_user_prompt(query))
        except Exception as exc:
            logger.warning("LLM 意图分类失败 query=%r err=%s", query, exc)
            return []
        if not raw or raw.startswith("__MOCK__"):
            return []
        items = _extract_json(raw)
        if not items:
            logger.debug("LLM 意图分类响应无法解析为 JSON: %s", raw[:200])
            return []
        return _parse_llm_items(items, query)
