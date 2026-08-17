# -*- coding: utf-8 -*-
"""AI 服务编排层（需求 3.X.1 / 3.X.2 / 3.X.4 整合入口）。

职责：
  1. 意图识别 -> 下游服务调用（聚合 / 算法 / 元数据）-> 文本生成
  2. 对上层 REST API 暴露统一调用契约；
  3. 不耦合 Flask，便于单测与服务复用。

下游服务依赖（通过构造注入，避免循环引用）：
  * AggregationService：多维度聚合查询（人员3）
  * AlgorithmService：复杂算法调度（人员3，statistics/association/cost_prediction/readmission_risk）
  * MetadataService：维度/指标/算法清单（人员3）
"""
from __future__ import annotations

import logging
from typing import Any

from app.ai.intent.catalog import INTENT_BY_KEY, IntentSpec, intent_meta
from app.ai.intent.classifier import IntentClassifier, IntentResult
from app.ai.intent.training_data import dataset_meta
from app.ai.summary.generator import SummaryGenerator, SummaryResult
from app.ai.summary.llm_client import LLMClient, build_client, describe_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 编排结果
# ---------------------------------------------------------------------------
class AIExecutionResult:
    """一次完整 AI 编排执行结果。"""

    def __init__(self, intent: IntentResult, analysis: Any,
                 summary: SummaryResult):
        self.intent = intent
        self.analysis = analysis
        self.summary = summary

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.to_dict(),
            "analysis": self.analysis,
            "summary": self.summary.to_dict(),
        }


# ---------------------------------------------------------------------------
# 服务编排
# ---------------------------------------------------------------------------
class AIService:
    """AI 智能层服务编排入口。

    使用：
        service = AIService(
            settings=settings,
            aggregation_service=agg_svc,
            algorithm_service=algo_svc,
        )
        # 单独意图识别
        result = service.recognize_intent("2021年各医院的平均费用")
        # 完整执行（识别+调用+生成）
        result = service.execute("2021年各医院的平均费用")
    """

    def __init__(
        self,
        settings,
        aggregation_service=None,
        algorithm_service=None,
        intent_classifier: IntentClassifier | None = None,
        summary_generator: SummaryGenerator | None = None,
        llm_client: LLMClient | None = None,
    ):
        self._settings = settings
        self._aggregation = aggregation_service
        self._algorithm = algorithm_service
        # 意图分类器
        self._classifier = intent_classifier or IntentClassifier(
            min_confidence=settings.intent_min_confidence)
        # LLM 客户端（注入或自动构建）
        self._client = llm_client or build_client(settings)
        # 文本生成器
        self._generator = summary_generator or SummaryGenerator(
            self._client, tolerance=settings.hallucination_tolerance)

    # ------------------------------------------------------------------
    # 1. 意图识别（单独暴露）
    # ------------------------------------------------------------------
    def recognize_intent(self, query: str) -> IntentResult:
        """对用户输入做意图识别，返回带参数与置信度的结果。"""
        return self._classifier.classify(query)

    # ------------------------------------------------------------------
    # 2. 完整执行（识别 + 调度 + 生成）
    # ------------------------------------------------------------------
    def execute(self, query: str) -> AIExecutionResult:
        """端到端：意图识别 -> 下游调用 -> 文本生成。"""
        # 1. 意图识别
        intent = self._classifier.classify(query)
        logger.info("AI 编排：query=%r intent=%s confidence=%.3f",
                    query, intent.intent, intent.confidence)

        # 2. 下游调度
        analysis = self._dispatch(intent)
        # 3. 文本生成
        summary = self._generator.generate(
            user_query=query,
            intent_label=intent.spec.label_cn,
            intent_key=intent.intent,
            analysis_result=analysis,
        )
        return AIExecutionResult(intent=intent, analysis=analysis, summary=summary)

    # ------------------------------------------------------------------
    # 3. 单独文本生成（用户已有分析结果）
    # ------------------------------------------------------------------
    def generate_summary(
        self,
        query: str,
        intent_label: str,
        intent_key: str,
        analysis_result: Any,
    ) -> SummaryResult:
        """直接对给定的分析结果生成摘要（不经过意图识别）。"""
        return self._generator.generate(
            user_query=query,
            intent_label=intent_label,
            intent_key=intent_key,
            analysis_result=analysis_result,
        )

    # ------------------------------------------------------------------
    # 4. AI 能力元数据
    # ------------------------------------------------------------------
    def meta(self) -> dict:
        """对外暴露 AI 能力元数据（供 /api/v1/ai/meta）。"""
        return {
            "intents": intent_meta(),
            "dataset": dataset_meta(),
            "llm": describe_client(self._client, self._settings),
        }

    # ==================================================================
    # 内部：下游调度
    # ==================================================================
    def _dispatch(self, intent: IntentResult) -> Any:
        """按意图分类调用下游服务。"""
        spec: IntentSpec = intent.spec
        try:
            if spec.downstream == "none":
                return None
            if spec.downstream == "aggregation":
                return self._call_aggregation(intent)
            if spec.downstream == "algorithm":
                return self._call_algorithm(intent)
            if spec.downstream == "metadata":
                return self._call_metadata(intent)
            logger.warning("未知 downstream=%s", spec.downstream)
            return None
        except Exception as e:
            logger.warning("下游调度失败 intent=%s err=%s", intent.intent, e)
            return {"error": str(e), "intent": intent.intent}

    # ------------------------------------------------------------------
    def _call_aggregation(self, intent: IntentResult) -> Any:
        """调用聚合服务。"""
        if self._aggregation is None:
            return {"error": "聚合服务未注入", "params": intent.params}
        params = dict(intent.params)
        # 默认指标
        if "metrics" not in params or not params["metrics"]:
            params["metrics"] = ["discharge_count"]
        if not params.get("dimensions"):
            # 无维度 -> 至少按年份聚合
            params["dimensions"] = ["discharge_year"]
        return self._aggregation.run(params)

    # ------------------------------------------------------------------
    def _call_algorithm(self, intent: IntentResult) -> Any:
        """调用算法服务（statistics/association/cost_prediction/readmission_risk）。"""
        if self._algorithm is None:
            return {"error": "算法服务未注入", "params": intent.params}
        if not intent.spec.target:
            return {"error": "算法目标未指定"}
        return self._algorithm.run(intent.spec.target, dict(intent.params))

    # ------------------------------------------------------------------
    def _call_metadata(self, intent: IntentResult) -> Any:
        """调用元数据服务（维度/指标/算法清单）。"""
        from app.algorithms.base import list_algorithms
        from config.registry import dimension_meta, metric_meta

        kind = intent.params.get("kind")
        if kind == "dimensions":
            return {"dimensions": dimension_meta()}
        if kind == "metrics":
            return {"metrics": metric_meta()}
        if kind == "algorithms":
            return {"algorithms": list_algorithms()}
        # 没指定 -> 全返回
        return {
            "dimensions": dimension_meta(),
            "metrics": metric_meta(),
            "algorithms": list_algorithms(),
        }


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_singleton: AIService | None = None


def get_ai_service(settings=None, **kwargs) -> AIService:
    """获取 AIService 单例（首次调用时按 settings 自动构建）。"""
    global _singleton
    if _singleton is None:
        if settings is None:
            from config.settings import Settings
            settings = Settings.load()
        _singleton = AIService(settings=settings, **kwargs)
    return _singleton


def reset_ai_service() -> None:
    """重置单例（测试用）。"""
    global _singleton
    _singleton = None
