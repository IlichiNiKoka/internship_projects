# -*- coding: utf-8 -*-
"""医疗洞察报告：多次分析组织、摘要调用、模板渲染与中立图表规格。"""

from __future__ import annotations

import copy
from typing import Any

from app.ai.summary.hallucination import check as check_numeric_consistency
from app.application.models import AnalysisRecord, new_id, utc_now


_INTENT_TITLES = {
    "aggregation_query": "多维度聚合分析",
    "statistics_overview": "总体指标概览",
    "association_analysis": "疾病关联洞察",
    "cost_prediction": "住院费用预测",
    "readmission_risk": "再入院风险分析",
    "metadata_query": "平台分析能力",
}


class ChartSpecBuilder:
    """从可信结构化数据确定性生成前端中立图表规格，不生成任意 JS。"""

    def build(self, record: AnalysisRecord) -> list[dict[str, Any]]:
        business = self._business(record)
        if not isinstance(business, dict):
            return []
        method = getattr(self, f"_{record.intent}", None)
        return method(record, business) if method else []

    @staticmethod
    def _business(record: AnalysisRecord) -> Any:
        envelope = record.result if isinstance(record.result, dict) else {}
        raw = envelope.get("data", envelope)
        if isinstance(raw, dict) and "algorithm" in raw and "result" in raw:
            return raw.get("result")
        return raw

    @staticmethod
    def _chart(record: AnalysisRecord, chart_type: str, title: str,
               dataset: list[dict], encoding: dict, series: list[dict]) -> dict:
        return {
            "chart_id": new_id("chart"),
            "analysis_id": record.id,
            "type": chart_type,
            "title": title,
            "dataset": copy.deepcopy(dataset[:200]),
            "encoding": encoding,
            "series": series,
        }

    def _aggregation_query(self, record, data):
        rows = data.get("rows") or []
        if not rows:
            return []
        dimensions = [
            item.get("column") or item.get("key")
            for item in data.get("dimensions") or [] if isinstance(item, dict)
        ]
        metrics = [
            item.get("key") for item in data.get("metrics") or [] if isinstance(item, dict)
        ]
        dimensions = [item for item in dimensions if item]
        metrics = [item for item in metrics if item]
        if not dimensions or not metrics:
            return []
        return [self._chart(
            record, "bar", "聚合指标分布", rows,
            {"category": dimensions, "values": metrics},
            [{"field": item, "name": item} for item in metrics],
        )]

    def _statistics_overview(self, record, data):
        charts = []
        for dimension, rows in list((data.get("distributions") or {}).items())[:4]:
            if rows:
                charts.append(self._chart(
                    record, "bar", f"{dimension} 分布", rows,
                    {"category": "value", "values": ["count"]},
                    [{"field": "count", "name": "住院人次"}],
                ))
        return charts

    def _association_analysis(self, record, data):
        rows = []
        for rule in data.get("rules") or []:
            left = next(iter((rule.get("antecedent") or {}).values()), "未知")
            right = next(iter((rule.get("consequent") or {}).values()), "未知")
            rows.append({
                "rule": f"{left} → {right}",
                "support": rule.get("support"),
                "confidence": rule.get("confidence"),
                "lift": rule.get("lift"),
            })
        if not rows:
            return []
        return [self._chart(
            record, "scatter", "关联规则强度", rows,
            {"x": "support", "y": "confidence", "size": "lift", "label": "rule"},
            [{"field": "confidence", "name": "关联规则"}],
        )]

    def _cost_prediction(self, record, data):
        if data.get("mode") == "predict" and data.get("predicted_total_charges") is not None:
            dataset = [{
                "name": "预测住院总费用",
                "value": data.get("predicted_total_charges"),
                "unit": data.get("currency", "USD"),
            }]
            return [self._chart(
                record, "kpi", "费用预测结果", dataset,
                {"label": "name", "value": "value"},
                [{"field": "value", "name": "预测费用"}],
            )]
        metrics = data.get("metrics") or {}
        dataset = [
            {"metric": key.upper(), "value": metrics.get(key)}
            for key in ("mae", "rmse", "r2") if metrics.get(key) is not None
        ]
        return [] if not dataset else [self._chart(
            record, "bar", "费用模型评估", dataset,
            {"category": "metric", "values": ["value"]},
            [{"field": "value", "name": "指标值"}],
        )]

    def _readmission_risk(self, record, data):
        if data.get("mode") == "score":
            if data.get("risk_score") is None:
                return []
            dataset = [{
                "risk_score": data.get("risk_score"),
                "risk_level": data.get("risk_level"),
            }]
            return [self._chart(
                record, "gauge", "再入院风险评分", dataset,
                {"value": "risk_score", "label": "risk_level"},
                [{"field": "risk_score", "name": "风险分"}],
            )]
        rows = data.get("level_distribution") or []
        return [] if not rows else [self._chart(
            record, "pie", "再入院风险等级分布", rows,
            {"category": "level", "values": ["count"]},
            [{"field": "count", "name": "记录数"}],
        )]


class MedicalReportService:
    """使用人员4摘要能力，并由人员1的固定模板输出结构化洞察报告。"""

    def __init__(self, summary_generator, max_analyses: int = 10):
        self._generator = summary_generator
        self._max_analyses = max(1, int(max_analyses))
        self._charts = ChartSpecBuilder()

    def generate(
        self,
        *,
        session_id: str,
        analyses: list[AnalysisRecord],
        title: str | None = None,
    ) -> dict[str, Any]:
        if not analyses:
            raise ValueError("生成报告至少需要一项已完成分析")
        selected = analyses[-self._max_analyses:]
        warnings: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []
        charts: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        trusted_narratives: list[str] = []

        for index, record in enumerate(selected, 1):
            summary_data = self._summary_data(record)
            summary = self._generate_summary(record, summary_data)
            hallucination = summary.get("hallucination") or {}
            narrative = str(summary.get("text") or "")

            # 数字一致性校验（需求 2.3）：本地确定性比对摘要文本中的数字与
            # 结构化分析结果，零额外 LLM 往返；不一致的摘要不纳入报告结论。
            consistency = check_numeric_consistency(summary_data, narrative)
            hallucination = {
                **(hallucination if isinstance(hallucination, dict) else {}),
                **consistency.to_dict(),
            }

            # 缺少校验信息必须视为不可信，医疗报告采用 fail-closed。
            trusted = self._is_trusted_summary(summary) and consistency.passed
            if not trusted:
                narrative = "该项自动摘要未通过数据一致性校验，请以本节结构化数据为准。"
                if not consistency.passed:
                    # 数字不一致：给出未匹配数字明细，便于前端/运维定位
                    detail = (
                        "自动摘要存在与结构化数据不一致的数字，已从报告结论排除"
                        f"（未匹配数字：{consistency.unmatched[:5]}）"
                    )
                else:
                    detail = "自动摘要未纳入报告结论"
                warnings.append({
                    "code": "UNTRUSTED_SUMMARY",
                    "analysis_id": record.id,
                    "message": detail,
                })
            elif summary.get("fell_back_to_mock"):
                warnings.append({
                    "code": "LLM_FALLBACK",
                    "analysis_id": record.id,
                    "message": "LLM 不可用，本节使用确定性模板摘要",
                })
            if summary.get("reused_saved_summary"):
                warnings.append({
                    "code": "SAVED_SUMMARY_REUSED",
                    "analysis_id": record.id,
                    "message": "摘要服务异常，本节复用此前已通过校验的摘要",
                })
            if trusted and narrative:
                trusted_narratives.append(narrative)

            section_charts = self._charts.build(record)
            charts.extend(section_charts)
            sections.append({
                "section_id": f"section_{index}",
                "analysis_id": record.id,
                "intent": record.intent,
                "title": _INTENT_TITLES.get(record.intent, "医疗分析"),
                "query": record.query,
                "narrative": narrative,
                "summary_validation": {
                    "trusted": trusted,
                    "llm_provider": summary.get("llm_provider"),
                    "fell_back_to_mock": bool(summary.get("fell_back_to_mock")),
                    "hallucination": hallucination,
                },
                "key_metrics": self._key_metrics(record, summary_data),
                "table": self._table(record),
                "chart_ids": [item["chart_id"] for item in section_charts],
            })
            result_envelope = record.result if isinstance(record.result, dict) else {}
            tool_provenance = result_envelope.get("provenance") or {}
            provenance.append({
                "analysis_id": record.id,
                "tool": record.tool_name,
                "tool_input": copy.deepcopy(record.tool_input),
                "attempts": record.attempts,
                "elapsed_seconds": record.elapsed_seconds,
                "called_at": tool_provenance.get("called_at") or record.created_at,
            })

        executive_summary = self._executive_summary(trusted_narratives, len(selected))
        normalized_title = str(title or "").strip() or "医疗大数据洞察报告"
        return {
            "schema_version": "1.0",
            "report_id": new_id("rpt"),
            "session_id": session_id,
            "title": normalized_title[:120],
            "generated_at": utc_now(),
            "source_analysis_ids": [item.id for item in selected],
            "executive_summary": executive_summary,
            "sections": sections,
            "charts": charts,
            "warnings": warnings,
            "validation": {
                "all_summaries_trusted": not any(
                    item["code"] == "UNTRUSTED_SUMMARY" for item in warnings
                ),
                "warning_count": len(warnings),
                "source_count": len(selected),
            },
            "provenance": provenance,
            "display": {
                "supported_exports": ["json"],
                "default_layout": "medical-insight-report-v1",
            },
        }

    def _generate_summary(self, record: AnalysisRecord, summary_data: Any) -> dict[str, Any]:
        try:
            result = self._generator.generate(
                user_query=record.query,
                intent_label=_INTENT_TITLES.get(record.intent, record.intent),
                intent_key=record.intent,
                analysis_result=summary_data,
            )
            return result.to_dict() if hasattr(result, "to_dict") else dict(result)
        except Exception:
            # 摘要服务异常时，复用此前已生成的摘要（防幻觉由提示词约束保证，
            # 不再做数字复校验）。
            saved = copy.deepcopy(record.summary or {})
            if self._is_trusted_summary(saved):
                saved["hallucination"] = dict(saved.get("hallucination") or {
                    "passed": True, "mode": "prompt_only",
                })
                saved["reused_saved_summary"] = True
                return saved
            return {
                "text": "摘要服务暂不可用，请查看结构化分析数据。",
                "llm_provider": "unavailable",
                "fell_back_to_mock": False,
                "empty_source": False,
                "hallucination": {
                    "passed": False,
                    "reason": "summary_generation_failed",
                },
            }

    @staticmethod
    def _is_trusted_summary(summary: Any) -> bool:
        """可信判定：有非空正文、非空源且显式失败标记未触发。

        数字一致性由 generate() 内的 check_numeric_consistency 单独把关；
        此处仅做基础门槛判断（生成服务异常等显式失败仍 fail-closed）。
        """
        if not isinstance(summary, dict):
            return False
        # 显式失败标记（如生成服务异常）仍然 fail-closed
        hallucination = summary.get("hallucination")
        if isinstance(hallucination, dict) and hallucination.get("passed") is False:
            return False
        return (
            not summary.get("empty_source")
            and bool(str(summary.get("text") or "").strip())
        )

    @staticmethod
    def _summary_data(record: AnalysisRecord) -> Any:
        """取摘要用扁平事实；兼容旧快照：无 summary_data/data 时回退到整个结果。"""
        if isinstance(record.result, dict):
            if "summary_data" in record.result:
                return record.result["summary_data"]
            if "data" in record.result:
                return record.result["data"]
            return record.result
        return record.result

    @staticmethod
    def _executive_summary(narratives: list[str], count: int) -> str:
        if not narratives:
            return f"本报告汇总 {count} 项医疗数据分析；自动摘要未通过校验，请查看结构化章节。"
        if count == 1:
            return narratives[0]
        # 不让报告层自行推断新结论，只拼接已通过人员4数字校验的摘要。
        return f"本报告汇总 {count} 项医疗数据分析。" + "\n".join(
            f"{idx}. {text}" for idx, text in enumerate(narratives, 1)
        )

    @staticmethod
    def _key_metrics(record: AnalysisRecord, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        keys_by_intent = {
            "statistics_overview": [
                ("total_discharges", "住院人次", "人次"),
                ("avg_length_of_stay", "平均住院时长", "天"),
                ("avg_total_charges", "平均总费用", "USD"),
                ("emergency_rate", "急诊占比", "%"),
            ],
            "association_analysis": [
                ("transaction_count", "参与分析记录", "人次"),
                ("rule_count", "关联规则数", "条"),
            ],
            "aggregation_query": [("row_count", "结果分组数", "组")],
        }
        mode = data.get("mode")
        if record.intent == "cost_prediction":
            if mode == "predict":
                keys = [("predicted_charge", "预测住院总费用", data.get("currency", ""))]
            elif mode == "train":
                keys = [("mae", "MAE", ""), ("rmse", "RMSE", ""), ("r2", "R²", "")]
            else:
                # 兼容旧结果未携带 mode 的情况，但仍只展示实际存在值。
                keys = [
                    ("predicted_charge", "预测住院总费用", data.get("currency", "")),
                    ("mae", "MAE", ""), ("rmse", "RMSE", ""), ("r2", "R²", ""),
                ]
        elif record.intent == "readmission_risk":
            if mode == "score":
                keys = [("risk_score", "风险评分", "分")]
            elif mode == "profile":
                keys = [("high_risk_rate", "高风险占比", "")]
            else:
                keys = [
                    ("high_risk_rate", "高风险占比", ""),
                    ("risk_score", "风险评分", "分"),
                ]
        else:
            keys = keys_by_intent.get(record.intent, [])
        result = []
        for key, label, unit in keys:
            if data.get(key) is not None:
                result.append({"key": key, "label": label, "value": data.get(key), "unit": unit})
        return result

    @staticmethod
    def _table(record: AnalysisRecord) -> dict[str, Any] | None:
        envelope = record.result if isinstance(record.result, dict) else {}
        raw = envelope.get("data", envelope)
        if isinstance(raw, dict) and "algorithm" in raw and "result" in raw:
            raw = raw.get("result")
        if not isinstance(raw, dict):
            return None
        if record.intent == "aggregation_query":
            rows = raw.get("rows") or []
        elif record.intent == "association_analysis":
            rows = raw.get("rules") or []
        elif record.intent == "statistics_overview":
            rows = raw.get("top_diseases") or []
        elif record.intent == "readmission_risk":
            rows = raw.get("avg_risk_by_age_admission") or raw.get("contributions") or []
        else:
            rows = []
        if not rows:
            return None
        columns = list(rows[0].keys()) if isinstance(rows[0], dict) else []
        return {"columns": columns, "rows": copy.deepcopy(rows[:100]), "truncated": len(rows) > 100}
