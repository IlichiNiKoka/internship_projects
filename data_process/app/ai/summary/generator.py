# -*- coding: utf-8 -*-
"""分析结果文本生成器（需求 3.X.2 主入口）。

流程（2026-08-24 性能优化：移除幻觉后置校验）：
  1. 输入：用户原始查询 + 意图识别结果 + 结构化分析结果
  2. 空数据 -> 返回「当前没有可用的分析数据。」
  3. 调用 LLM 生成文本（API key 留空 -> Mock 模板渲染）
  4. 直接返回原文。

性能说明：
  旧版在生成后会再做「本地数字比对 + LLM 事实核查」，每次摘要多一次完整
  LLM 往返，延迟接近翻倍。现改为纯提示词约束防幻觉（见 prompts.py 的
  SYSTEM_PROMPT 强约束规则），单次查询只调一次 LLM。

输出契约：
  SummaryResult{text, hallucination, llm_provider, fell_back_to_mock}
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.ai.summary.llm_client import LLMClient
from app.ai.summary.prompts import (
    MOCK_TEMPLATES,
    SYSTEM_PROMPT,
    build_user_prompt,
    mock_render,
)

logger = logging.getLogger(__name__)

# 注入 Prompt 的结构化结果上限：rows 类列表只保留前 N 行（数据已按指标
# 降序排序，LLM 只需要头部趋势即可作答），并将截断信息附在 Prompt 中。
PROMPT_MAX_ROWS = 50
# 浮点压缩精度：Prompt 中的数字统一保留 4 位小数，减少 token 数。
PROMPT_FLOAT_DIGITS = 4
# 摘要缓存 TTL（秒）：相同 意图+提问+分析结果 不再重复调用 LLM。
SUMMARY_CACHE_TTL = 1800


@dataclass
class SummaryResult:
    """文本生成统一输出。"""
    text: str                                  # 生成文本
    llm_provider: str                          # 使用的 LLM 客户端
    fell_back_to_mock: bool = False           # 是否降级到了 Mock 模板
    hallucination: dict = field(default_factory=dict)  # 兼容字段：恒为 prompt_only 模式
    empty_source: bool = False                # 源分析结果是否为空

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "llm_provider": self.llm_provider,
            "fell_back_to_mock": self.fell_back_to_mock,
            "empty_source": self.empty_source,
            "hallucination": self.hallucination,
        }


# ---------------------------------------------------------------------------
# 生成器
# ---------------------------------------------------------------------------
# 兼容性占位：下游（application/reports 等）仍读取 summary["hallucination"]，
# 统一返回该标记，表示「无后置校验，防幻觉由系统提示词强约束保证」。
PROMPT_ONLY_REPORT = {
    "passed": True,
    "mode": "prompt_only",
    "note": "后置幻觉校验已移除，防幻觉由内置提示词强约束保证",
}


class SummaryGenerator:
    """分析结果文本生成器。

    使用：
        generator = SummaryGenerator(client=client)
        result = generator.generate(query, intent_label, analysis_result)
    """

    def __init__(self, client: LLMClient, cache=None,
                 cache_ttl_seconds: int = SUMMARY_CACHE_TTL):
        self._client = client
        # 摘要缓存（可选）：复用核心缓存后端（InMemoryTTLCache / RedisCacheBackend），
        # key 前缀 summary: 与聚合缓存隔离。相同「意图+提问+分析结果」直接命中，
        # 跳过 LLM 往返（演示/追问场景收益最大）。
        self._cache = cache
        self._cache_ttl = max(1, int(cache_ttl_seconds))

    def generate(self, user_query: str, intent_label: str,
                 intent_key: str, analysis_result: Any) -> SummaryResult:
        """生成自然语言摘要。"""
        # ---- 1. 空数据特判 ----
        if not analysis_result or self._is_empty(analysis_result):
            return SummaryResult(
                text="当前没有可用的分析数据。",
                llm_provider=self._client.provider,
                empty_source=True,
                hallucination=dict(PROMPT_ONLY_REPORT),
            )

        # ---- 1.5 Prompt 数据裁剪 + 摘要缓存 ----
        pruned, truncated_rows = self._prune_for_prompt(analysis_result)
        analysis_json = json.dumps(pruned, ensure_ascii=False, default=str)
        user_prompt = build_user_prompt(user_query, intent_label,
                                        pruned, analysis_json)
        if truncated_rows:
            user_prompt += (
                f"\n【说明】原始分析结果共 {truncated_rows} 行，为控制长度仅注入"
                f"前 {PROMPT_MAX_ROWS} 行；请基于已注入数据客观作答，"
                "不得编造被截断部分的具体数值。"
            )

        cache_key = None
        if self._cache is not None:
            digest = hashlib.sha1(
                f"{intent_key}\n{user_query}\n{analysis_json}".encode("utf-8")
            ).hexdigest()
            cache_key = f"summary:{digest}"
            cached = self._cache.get(cache_key)
            if isinstance(cached, dict) and cached.get("text"):
                logger.info("摘要命中缓存 key=%s", cache_key)
                return SummaryResult(
                    text=cached["text"],
                    llm_provider=cached.get("llm_provider", self._client.provider),
                    fell_back_to_mock=bool(cached.get("fell_back_to_mock")),
                    hallucination=dict(cached.get("hallucination")
                                       or PROMPT_ONLY_REPORT),
                )

        # ---- 2. 调用 LLM ----
        fell_back = False
        try:
            llm_text = self._client.chat(SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            logger.warning("LLM 调用异常，降级到 Mock：%s", e)
            llm_text = ""
            fell_back = True

        # ---- 3. Mock 兜底渲染（用完整结果，保证模板参数齐全）----
        if not llm_text or llm_text.startswith("__MOCK__"):
            fell_back = True
            llm_text = self._mock_render(intent_key, analysis_result)

        result = SummaryResult(
            text=llm_text,
            llm_provider=self._client.provider,
            fell_back_to_mock=fell_back,
            hallucination=dict(PROMPT_ONLY_REPORT),
        )

        # ---- 4. 写摘要缓存 ----------------
        # 只缓存真实 LLM 结果：Mock 兜底是 LLM 失败/超时时的降级产物，
        # 若一并缓存（默认 TTL 30 分钟），一次瞬时超时会让后续所有相同
        # 提问都返回模板文本，表现为「分析失效」。兜底不缓存，下次重试。
        if cache_key is not None and not fell_back:
            try:
                self._cache.set(cache_key, result.to_dict(), self._cache_ttl)
            except Exception as e:  # noqa: BLE001 —— 缓存写入失败不影响主流程
                logger.warning("摘要缓存写入失败 key=%s err=%s", cache_key, e)
        return result

    # ------------------------------------------------------------------
    def _prune_for_prompt(self, value: Any) -> tuple[Any, int | None]:
        """裁剪注入 Prompt 的结构化结果。

        返回 (裁剪后的值, 被截断的原始行数或 None)：
          * 浮点数压缩到 4 位小数（减少 token，同时保留精度避免误导）；
          * 长度超过 PROMPT_MAX_ROWS 的列表截断到前 N 行；
          * 递归作用于 dict/list，rows 等行式数据同样生效。
        """
        if value is None or isinstance(value, (bool, int, str)):
            return value, None
        if isinstance(value, float):
            return round(value, PROMPT_FLOAT_DIGITS), None
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            truncated: int | None = None
            for k, v in value.items():
                pv, pt = self._prune_for_prompt(v)
                out[k] = pv
                if pt is not None:
                    truncated = pt
            return out, truncated
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) > PROMPT_MAX_ROWS:
                return [self._prune_for_prompt(v)[0] for v in items[:PROMPT_MAX_ROWS]], \
                    len(items)
            return [self._prune_for_prompt(v)[0] for v in items], None
        return value, None

    # ------------------------------------------------------------------
    def _mock_render(self, intent_key: str, analysis_result: Any) -> str:
        """根据分析结果结构抽取关键字段并喂给 Mock 模板。"""
        params = self._extract_template_params(intent_key, analysis_result)
        return mock_render(intent_key, params)

    @staticmethod
    def _is_empty(result: Any) -> bool:
        """判断分析结果是否视为「空」。"""
        if result is None:
            return True
        if isinstance(result, (list, tuple, dict, set)):
            return len(result) == 0
        if isinstance(result, (int, float)):
            return result == 0
        if isinstance(result, str):
            return result.strip() == ""
        return False

    # ------------------------------------------------------------------
    def _extract_template_params(self, intent_key: str, result: Any) -> dict:
        """从分析结果中抽取模板需要的字段（尽力而为，缺则填占位符）。"""
        params: dict = {}
        try:
            if intent_key == "aggregation_query":
                # 期望 result = {"rows": [[dim1, metric1], ...], ...}
                rows = self._get(result, "rows") or (result if isinstance(result, list) else [])
                dims = self._get(result, "dimensions") or ["未指定维度"]
                if isinstance(dims, (list, tuple)):
                    params["dimensions"] = " / ".join(str(d) for d in dims)
                else:
                    params["dimensions"] = str(dims)
                params["row_count"] = len(rows) if isinstance(rows, list) else 0
                if isinstance(rows, list) and rows:
                    first_row = rows[0]
                    # 取最后一列作为「最大分组的指标值」，避免输出原始字段值字符串
                    if isinstance(first_row, (list, tuple)) and len(first_row) > 0:
                        params["top_row"] = str(first_row[-1])
                    else:
                        params["top_row"] = str(first_row)
                else:
                    params["top_row"] = "无数据"
            elif intent_key == "statistics_overview":
                params["total_discharges"] = self._get(result, "total_discharges") or 0
                params["avg_los"] = self._get(result, "avg_length_of_stay") or 0
                params["avg_charges"] = self._get(result, "avg_total_charges") or 0
                params["emergency_rate"] = self._get(result, "emergency_rate") or 0
            elif intent_key == "association_analysis":
                rules = self._get(result, "rules") or []
                params["rule_count"] = len(rules)
                if rules:
                    top = rules[0] if isinstance(rules, list) else {}
                    params["top_rule"] = self._get(top, "rule_str") or str(top)
                    params["support"] = self._get(top, "support") or 0
                    params["confidence"] = self._get(top, "confidence") or 0
                else:
                    params["mode"] = "empty"
            elif intent_key == "cost_prediction":
                mode = self._get(result, "mode")
                params["mode"] = mode
                if mode == "train":
                    for key in ("mae", "rmse", "r2"):
                        value = self._get(result, key)
                        if value is not None:
                            params[key] = value
                elif mode == "predict":
                    value = self._get(result, "predicted_charge")
                    if value is not None:
                        params["predicted_charge"] = value
                    params["currency"] = self._get(result, "currency") or "USD"
            elif intent_key == "readmission_risk":
                mode = self._get(result, "mode")
                params["mode"] = mode
                if mode == "profile":
                    rate = self._get(result, "high_risk_rate")
                    if rate is not None:
                        params["high_risk_rate"] = rate
                    params["top_group"] = self._get(
                        result, "top_risk_group"
                    ) or "未知人群"
                elif mode == "score":
                    score = self._get(result, "risk_score")
                    if score is not None:
                        params["risk_score"] = score
                    params["risk_level"] = self._get(result, "risk_level") or "Unknown"
            elif intent_key == "metadata_query":
                item_count = 0
                if isinstance(result, dict):
                    for v in result.values():
                        if isinstance(v, (list, dict)):
                            item_count += len(v)
                        else:
                            item_count += 1
                elif isinstance(result, list):
                    item_count = len(result)
                params["item_count"] = item_count
        except Exception as e:
            logger.warning("Mock 模板参数抽取失败 intent=%s err=%s", intent_key, e)
        return params

    @staticmethod
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        """安全取 dict 字段。"""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return default
