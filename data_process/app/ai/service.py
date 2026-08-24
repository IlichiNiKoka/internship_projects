# -*- coding: utf-8 -*-
"""AI 服务编排层（Agent 模式）。

架构（用户要求 2026-08-24）：
  1. 取消本地"意图识别"前置步骤，输入直接交给 LLM 工具规划 Agent。
     LLM 全权决定调用 aggregation Spark 接口 / algorithm 算法 / metadata 元数据，
     或直接 grounded 文本回答。
  2. 不再生成裸 SQL。之前的 text_to_sql 功能改为让 LLM 直接输出 Spark aggregation
     的结构化参数（dimensions/metrics/filters/sort/limit），由本地调用 AggregationService，
     完全复用 Spark 的数据加载 cache、安全白名单、慢查询告警。
  3. 本地只负责：
     - 工具契约 prompt steering（维度/指标白名单、算法参数模板）
     - LLM 输出 JSON 解析 + 第二道参数校验与修复
     - 工具调用，统一超时/异常；最多 2 轮 self-correct
     - 最终 grounded 摘要生成（generator）

下游服务依赖（通过构造注入，避免循环引用）：
  * AggregationService：多维度聚合查询（人员3）
  * AlgorithmService：复杂算法调度（人员3，statistics/association/cost_prediction/readmission_risk）
"""
from __future__ import annotations

import logging
from typing import Any

from app.ai.agent import PlanResult, ToolPlanningAgent
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
        *,
        max_correct_loops: int = 2,
    ):
        self._settings = settings
        self._aggregation = aggregation_service
        self._algorithm = algorithm_service
        # 保留 classifier 给 recognize_intent 向后兼容；execute 不再使用
        self._classifier = intent_classifier or IntentClassifier(
            min_confidence=settings.intent_min_confidence)
        # LLM 客户端（注入或自动构建）
        self._client = llm_client or build_client(settings)
        # 文本生成器（幻觉后置校验已移除，防幻觉靠提示词强约束，少一次 LLM 往返）
        self._generator = summary_generator or SummaryGenerator(self._client)
        # Agent：LLM 直接做工具规划
        self._agent = ToolPlanningAgent(
            llm_client=self._client,
            aggregation_service=self._aggregation,
            algorithm_service=self._algorithm,
            max_correct_loops=max_correct_loops,
        )

    # ------------------------------------------------------------------
    # 1. 意图识别（单独暴露，保留用于 /api/v1/ai/intent 向后兼容）
    # ------------------------------------------------------------------
    def recognize_intent(self, query: str) -> IntentResult:
        """对用户输入做意图识别（规则引擎，仅供元数据与调试）。"""
        return self._classifier.classify(query)

    # ------------------------------------------------------------------
    # 2. 完整执行：Agent 工具规划 + 工具执行 + 文本生成
    # ------------------------------------------------------------------
    def execute(self, query: str, *, history: str = "") -> AIExecutionResult:
        """端到端：LLM 工具规划 → Spark/算法执行 → grounded 摘要。

        不再先做本地意图识别；不再生成裸 SQL，全部走 Spark aggregation 白名单。
        """
        plan: PlanResult = self._agent.plan_and_execute(query, history=history)
        logger.info(
            "Agent 规划完成 query=%r calls=%s loops_reached=%s plan_el=%.2fs exec_el=%.2fs",
            query, [c.tool for c in plan.calls], plan.max_loops_reached,
            plan.elapsed_planning_seconds, plan.elapsed_execution_seconds,
        )

        intent = self._plan_to_intent_result(query, plan)
        analysis = self._plan_to_analysis(query, plan)

        # 文本生成：direct_answer 走特殊路径（LLM 自己写了 answer）
        if plan.direct_answer is not None:
            # 直接构造 SummaryResult 风格，绕开对结构化数据的幻觉校验
            from dataclasses import asdict as _asdict
            text = plan.direct_answer or "当前无法基于数据回答此问题。"
            summary = self._generator.generate(
                user_query=query,
                intent_label="对话与边界说明",
                intent_key="direct_answer",
                analysis_result={"mode": "direct_answer", "answer": text},
            )
        else:
            intent_key = intent.intent or "freeform_query"
            intent_label = intent.spec.label_cn if intent.spec else "自由形式数据问答"
            summary = self._generator.generate(
                user_query=query,
                intent_label=intent_label,
                intent_key=intent_key,
                analysis_result=analysis,
            )

        return AIExecutionResult(intent=intent, analysis=analysis, summary=summary)

    # ------------------------------------------------------------------
    def _plan_to_intent_result(self, query: str, plan: PlanResult) -> IntentResult:
        """把 PlanResult 映射回旧的 IntentResult，让上层 API 契约不变。"""
        if not plan.calls:
            return IntentResult(
                query=query, intent="unsupported", confidence=0.1,
                params={}, matched_signals={"reason": plan.errors or ["empty_plan"]},
            )
        call = plan.calls[0]
        params = dict(call.params or {})
        if call.explanation:
            params["_agent_explanation"] = call.explanation
        if plan.errors:
            params["_agent_errors"] = list(plan.errors)

        # 工具 -> intent key 映射
        mapping = {
            "aggregation": "freeform_query",
            "algorithm": self._alg_to_intent_key((call.params or {}).get("name")),
            "metadata": "metadata_query",
            "direct_answer": "unsupported",
        }
        intent_key = mapping.get(call.tool, "unsupported")
        confidence = 0.9 if not plan.max_loops_reached else 0.5
        signals: dict[str, Any] = {
            "agent_tool": [call.tool],
            "plan_elapsed_s": [plan.elapsed_planning_seconds],
            "exec_elapsed_s": [plan.elapsed_execution_seconds],
        }
        return IntentResult(
            query=query, intent=intent_key, confidence=confidence,
            params=params, matched_signals=signals,
        )

    # ------------------------------------------------------------------
    def _plan_to_analysis(self, query: str, plan: PlanResult) -> Any:
        return plan.combined_analysis()

    # ------------------------------------------------------------------
    @staticmethod
    def _alg_to_intent_key(name: str | None) -> str:
        return {
            "statistics_overview": "statistics_overview",
            "association": "association_analysis",
            "cost_prediction": "cost_prediction_query",
            "readmission_risk": "readmission_risk_query",
            "group_aggregation": "aggregation_query",
        }.get(name or "", "freeform_query")

    # ------------------------------------------------------------------
    # 向后兼容：execute_multi 退化为单次 execute + 旧接口包装
    # ------------------------------------------------------------------
    def execute_multi(self, query: str) -> list[AIExecutionResult]:
        """Agent 模式下不做显式多意图分拆，交给 LLM 规划时合并处理。
        为了接口契约不崩返回长度至少为 1 的列表。
        """
        res = self.execute(query)
        return [res]

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
        """调用聚合服务（aggregation_query / freeform_query 统一入口）。"""
        if self._aggregation is None:
            return {"error": "聚合服务未注入", "params": intent.params}
        params = dict(intent.params)
        # 默认指标
        if "metrics" not in params or not params["metrics"]:
            params["metrics"] = ["discharge_count", "total_charges_mean", "length_of_stay_mean"]
        if not params.get("dimensions"):
            # 无维度：freeform_query 通常用户希望看全貌 -> 先按年份+医院双维度展开
            params["dimensions"] = ["discharge_year", "facility_name"]
            params["_note"] = "freeform_default_dimensions"
        # top_n / limit 支持
        if "top_n" in params and "limit" not in params:
            params["limit"] = int(params["top_n"])
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