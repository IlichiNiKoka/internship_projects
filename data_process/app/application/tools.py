# -*- coding: utf-8 -*-
"""智能工具注册、意图映射、参数转换、重试与结果组装。"""

from __future__ import annotations

import copy
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.application.clients import AnalysisAPIError, AnalysisClient
from app.application.models import utc_now


class ToolParameterError(ValueError):
    """意图已识别，但工具所需的业务参数仍不完整或不合法。"""

    def __init__(self, message: str, *, missing: list[str] | None = None):
        super().__init__(message)
        self.missing = missing or []


class ToolInvocationError(RuntimeError):
    """工具在重试后仍失败；不直接暴露下游异常堆栈或敏感配置。"""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str,
        attempts: int,
        status_code: int | None = None,
        trace_id: str | None = None,
    ):
        super().__init__(message)
        self.tool_name = tool_name
        self.attempts = attempts
        self.status_code = status_code
        self.trace_id = trace_id


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.1
    max_delay_seconds: float = 1.0

    def delay_after(self, failed_attempt: int) -> float:
        delay = self.base_delay_seconds * (2 ** max(0, failed_attempt - 1))
        return min(max(0.0, delay), max(0.0, self.max_delay_seconds))


@dataclass
class ToolCallResult:
    intent: str
    tool_name: str
    params: dict[str, Any]
    raw_result: Any
    summary_data: Any
    attempts: int
    elapsed_seconds: float
    called_at: str = field(default_factory=utc_now)

    def assembled_result(self) -> dict[str, Any]:
        """供会话/报告复用的稳定结果外壳，保留原始数据与摘要适配数据。"""
        return {
            "intent": self.intent,
            "tool": self.tool_name,
            "request": copy.deepcopy(self.params),
            "data": copy.deepcopy(self.raw_result),
            "summary_data": copy.deepcopy(self.summary_data),
            "provenance": {
                "source": "personnel3-analysis-api",
                "attempts": self.attempts,
                "elapsed_seconds": self.elapsed_seconds,
                "called_at": self.called_at,
            },
        }


@dataclass
class MedicalTool:
    name: str
    intent: str
    description: str
    handler: Callable[[dict[str, Any]], Any]
    parameter_adapter: Callable[[dict[str, Any]], dict[str, Any]]

    def invoke(self, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], Any]:
        normalized = self.parameter_adapter(dict(params or {}))
        return normalized, self.handler(normalized)

    def meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "intent": self.intent,
            "description": self.description,
        }


class ParameterAdapter:
    """把人员4意图参数转换为人员3接口的严格契约。"""

    DIMENSIONS = {
        "discharge_year", "age_group", "gender", "race", "ethnicity",
        "zip_code_3_digits", "hospital_service_area", "hospital_county",
        "facility_name", "ccsr_diagnosis_code", "ccsr_diagnosis_description",
        "ccsr_procedure_description", "apr_drg_description", "apr_mdc_description",
        "apr_severity_of_illness_description", "apr_risk_of_mortality",
        "apr_medical_surgical_description", "type_of_admission",
        "patient_disposition", "emergency_department_indicator",
        "payment_typology_1", "payment_typology_2", "payment_typology_3",
    }
    NUMERIC_FILTERS = {
        "length_of_stay", "total_charges", "total_costs", "birth_weight",
        "apr_severity_of_illness_code",
    }
    METRICS = {
        "discharge_count", "avg_length_of_stay", "max_length_of_stay",
        "avg_total_charges", "sum_total_charges", "avg_total_costs",
        "sum_total_costs", "avg_birth_weight", "avg_severity_of_illness",
    }
    ASSOCIATION_FIELDS = {
        "ccsr_diagnosis_description", "ccsr_diagnosis_code",
        "ccsr_procedure_description", "ccsr_procedure_code",
        "apr_mdc_description", "apr_severity_of_illness_description",
        "type_of_admission", "payment_typology_1", "age_group",
    }
    COST_SAMPLE_MAP = {
        "length_of_stay": "length_of_stay",
        "severity_code": "severity_code",
        "apr_severity_of_illness_code": "severity_code",
        "age_group": "age_group",
        "admission_type": "admission_type",
        "type_of_admission": "admission_type",
        "payment_type": "payment_type",
        "payment_typology_1": "payment_type",
        "medical_surgical": "medical_surgical",
        "apr_medical_surgical_description": "medical_surgical",
    }
    SEVERITY_CODES = {"Minor": 1, "Moderate": 2, "Major": 3, "Extreme": 4}
    RISK_SAMPLE_FIELDS = {
        "age_group",
        "type_of_admission",
        "apr_severity_of_illness_description",
        "apr_risk_of_mortality",
        "length_of_stay",
        "patient_disposition",
    }

    def __init__(self, default_limit: int = 100, max_limit: int = 1000):
        self._default_limit = max(1, int(default_limit))
        self._max_limit = max(1, int(max_limit))

    def aggregation(self, params: dict[str, Any]) -> dict[str, Any]:
        dimensions = self._unique(
            self._sequence(params.get("dimensions") or [], "dimensions")
        )
        metrics = self._unique(
            self._sequence(
                params.get("metrics") or ["discharge_count"], "metrics"
            )
        )
        invalid_dims = [item for item in dimensions if item not in self.DIMENSIONS]
        invalid_metrics = [item for item in metrics if item not in self.METRICS]
        if invalid_dims:
            raise ToolParameterError(f"不支持的分析维度: {invalid_dims}")
        if invalid_metrics:
            raise ToolParameterError(f"不支持的分析指标: {invalid_metrics}")

        filters = self._filters(
            self._sequence(params.get("filters") or [], "filters")
        )
        # 年份/性别等已被 eq 固定时，不再作为冗余分组维度；至少保留一个维度。
        fixed_fields = {
            item["field"] for item in filters if item.get("op") == "eq"
        }
        if len(dimensions) > 1:
            dimensions = [item for item in dimensions if item not in fixed_fields]
        if not dimensions:
            dimensions = ["discharge_year"]
        if len(dimensions) > 5:
            raise ToolParameterError("一次分析最多支持 5 个组合维度")

        limit = params.get("limit", self._default_limit)
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ToolParameterError("limit 必须是整数") from exc
        limit = min(max(1, limit), self._max_limit)

        sort = []
        selected_fields = set(dimensions) | set(metrics)
        for item in self._sequence(params.get("sort") or [], "sort"):
            if not isinstance(item, dict):
                raise ToolParameterError("sort 中每项必须是对象")
            field_name = item.get("field")
            order = item.get("order", "desc")
            if field_name not in selected_fields:
                raise ToolParameterError(f"排序字段不在本次结果中: {field_name}")
            if order not in {"asc", "desc"}:
                raise ToolParameterError(f"不支持的排序方向: {order}")
            sort.append({"field": field_name, "order": order})

        return {
            "dimensions": dimensions,
            "metrics": metrics,
            "filters": filters,
            "sort": sort,
            "limit": limit,
        }

    def statistics(self, params: dict[str, Any]) -> dict[str, Any]:
        top_n = params.get("top_n", 10)
        try:
            top_n = int(top_n)
        except (TypeError, ValueError) as exc:
            raise ToolParameterError("top_n 必须是整数") from exc
        return {"top_n": min(max(1, top_n), 50)}

    def association(self, params: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in ("antecedent", "consequent"):
            if params.get(key) is not None:
                field_name = str(params[key])
                if field_name not in self.ASSOCIATION_FIELDS:
                    raise ToolParameterError(f"关联分析不支持字段: {field_name}")
                result[key] = field_name
        if params.get("min_support") is not None:
            result["min_support"] = self._float_in_range(
                params["min_support"], "min_support", minimum=1e-6, maximum=1.0
            )
        if params.get("top_n") is not None:
            result["top_n"] = self._int_in_range(
                params["top_n"], "top_n", minimum=1, maximum=200
            )
        if (
            result.get("antecedent") is not None
            and result.get("antecedent") == result.get("consequent")
        ):
            raise ToolParameterError("关联分析的前件与后件不能相同")
        return result

    def cost_prediction(self, params: dict[str, Any]) -> dict[str, Any]:
        mode = str(params.get("mode") or "train")
        if mode not in {"train", "predict"}:
            raise ToolParameterError("费用预测 mode 只能是 train 或 predict")
        result: dict[str, Any] = {"mode": mode}
        if params.get("sample_size") is not None:
            result["sample_size"] = self._int_in_range(
                params["sample_size"], "sample_size", minimum=1000, maximum=1_000_000
            )
        if params.get("train_ratio") is not None:
            result["train_ratio"] = self._float_in_range(
                params["train_ratio"], "train_ratio", minimum=0.5, maximum=0.95
            )

        source_sample = params.get("sample") or {}
        if not isinstance(source_sample, dict):
            raise ToolParameterError("费用预测 sample 必须是对象")
        sample: dict[str, Any] = {}
        for source_key, target_key in self.COST_SAMPLE_MAP.items():
            if source_key in source_sample and source_sample[source_key] is not None:
                sample[target_key] = source_sample[source_key]
        severity = source_sample.get("apr_severity_of_illness_description")
        if "severity_code" not in sample and severity in self.SEVERITY_CODES:
            sample["severity_code"] = self.SEVERITY_CODES[severity]
        for numeric in ("length_of_stay", "severity_code"):
            if numeric in sample:
                maximum = 4 if numeric == "severity_code" else None
                sample[numeric] = self._int_in_range(
                    sample[numeric], numeric, minimum=1, maximum=maximum
                )
        if sample:
            result["sample"] = sample
        if mode == "predict" and not sample:
            raise ToolParameterError(
                "费用预测需要住院时长、年龄组等样本特征",
                missing=["sample"],
            )
        return result

    def readmission_risk(self, params: dict[str, Any]) -> dict[str, Any]:
        mode = str(params.get("mode") or "profile")
        if mode not in {"profile", "score"}:
            raise ToolParameterError("再入院风险 mode 只能是 profile 或 score")
        result: dict[str, Any] = {"mode": mode}
        sample = params.get("sample")
        if sample is not None:
            if not isinstance(sample, dict):
                raise ToolParameterError("风险评估 sample 必须是对象")
            # 只保留下游规则引擎真正使用的脱敏特征，避免将任意
            # LangChain 参数（包括潜在 PII）持久化到会话和报告。
            normalized_sample = {
                key: copy.deepcopy(value)
                for key, value in sample.items()
                if key in self.RISK_SAMPLE_FIELDS and value is not None
            }
            if normalized_sample.get("length_of_stay") is not None:
                normalized_sample["length_of_stay"] = self._float_in_range(
                    normalized_sample["length_of_stay"],
                    "sample.length_of_stay",
                    minimum=0,
                )
            for key, value in normalized_sample.items():
                if key != "length_of_stay" and isinstance(
                    value, (dict, list, tuple, set)
                ):
                    raise ToolParameterError(f"sample.{key} 必须是标量值")
            if normalized_sample:
                result["sample"] = normalized_sample
        if mode == "score" and not result.get("sample"):
            raise ToolParameterError(
                "单条风险评估需要患者特征", missing=["sample"]
            )
        return result

    @staticmethod
    def metadata(params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind")
        if kind not in {None, "dimensions", "metrics", "algorithms"}:
            raise ToolParameterError(f"不支持的元数据类型: {kind}")
        return {} if kind is None else {"kind": kind}

    def _filters(self, filters: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in filters:
            if not isinstance(item, dict):
                raise ToolParameterError("filters 中每项必须是对象")
            field_name = item.get("field")
            op = item.get("op")
            if field_name not in self.DIMENSIONS | self.NUMERIC_FILTERS:
                raise ToolParameterError(f"不支持的过滤字段: {field_name}")
            allowed_ops = {"eq", "ne", "in", "not_in"}
            if field_name in self.NUMERIC_FILTERS or field_name == "discharge_year":
                allowed_ops |= {"gte", "gt", "lte", "lt", "between"}
            if op not in allowed_ops:
                raise ToolParameterError(f"字段 {field_name} 不支持操作符 {op}")
            out = {"field": field_name, "op": op}
            if op in {"in", "not_in", "between"}:
                values = item.get("values")
                if not isinstance(values, list) or not values:
                    raise ToolParameterError(f"操作符 {op} 需要非空 values")
                if op == "between" and len(values) != 2:
                    raise ToolParameterError("between 需要两个边界值")
                out["values"] = copy.deepcopy(values)
            else:
                if item.get("value") is None:
                    raise ToolParameterError(f"操作符 {op} 需要 value")
                out["value"] = item["value"]
            fingerprint = json.dumps(out, sort_keys=True, ensure_ascii=False, default=str)
            if fingerprint not in seen:
                seen.add(fingerprint)
                normalized.append(out)
        return normalized

    @staticmethod
    def _unique(values: list[Any]) -> list[str]:
        return list(dict.fromkeys(str(item) for item in values if item is not None))

    @staticmethod
    def _sequence(value: Any, name: str) -> list[Any]:
        if not isinstance(value, (list, tuple)):
            raise ToolParameterError(f"{name} 必须是数组")
        return list(value)

    @staticmethod
    def _int_in_range(
        value: Any,
        name: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """把分类器输出转换成整数，并在调用下游前统一报告参数错误。"""
        try:
            # ``int(1.5)`` 会静默截断，字符串小数也不是合法整数。
            if isinstance(value, bool):
                raise ValueError
            converted = int(value)
            if not isinstance(value, str) and converted != value:
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise ToolParameterError(f"{name} 必须是整数") from exc
        if minimum is not None and converted < minimum:
            raise ToolParameterError(f"{name} 不能小于 {minimum}")
        if maximum is not None and converted > maximum:
            raise ToolParameterError(f"{name} 不能大于 {maximum}")
        return converted

    @staticmethod
    def _float_in_range(
        value: Any,
        name: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """转换有限浮点数；拒绝 NaN/Infinity，避免绕过范围检查。"""
        try:
            if isinstance(value, bool):
                raise ValueError
            converted = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ToolParameterError(f"{name} 必须是数字") from exc
        if not math.isfinite(converted):
            raise ToolParameterError(f"{name} 必须是有限数字")
        if minimum is not None and converted < minimum:
            raise ToolParameterError(f"{name} 不能小于 {minimum}")
        if maximum is not None and converted > maximum:
            raise ToolParameterError(f"{name} 不能大于 {maximum}")
        return converted


class ToolRegistry:
    """显式白名单工具注册表；同一意图只能映射到一个主工具。"""

    def __init__(self):
        self._by_name: dict[str, MedicalTool] = {}
        self._by_intent: dict[str, MedicalTool] = {}

    def register(self, tool: MedicalTool) -> None:
        if tool.name in self._by_name:
            raise ValueError(f"工具名重复: {tool.name}")
        if tool.intent in self._by_intent:
            raise ValueError(f"意图已映射工具: {tool.intent}")
        self._by_name[tool.name] = tool
        self._by_intent[tool.intent] = tool

    def for_intent(self, intent: str) -> MedicalTool | None:
        return self._by_intent.get(intent)

    def metadata(self) -> list[dict[str, Any]]:
        return [item.meta() for item in self._by_name.values()]

    def as_langchain_tools(self, executor: "ToolExecutor | None" = None) -> list[Any]:
        """返回真正的 LangChain ``StructuredTool``；未安装依赖时返回空列表。"""
        try:
            from langchain_core.tools import StructuredTool
            from pydantic import BaseModel, Field
        except ImportError:
            return []

        class ToolArgs(BaseModel):
            params: dict[str, Any] = Field(
                default_factory=dict, description="意图识别后得到的结构化工具参数"
            )

        result = []
        for tool in self._by_name.values():
            # 默认参数绑定当前循环对象，避免 Python 闭包晚绑定。
            def invoke(params: dict[str, Any] | None = None, _tool=tool):
                if executor is not None:
                    return executor.execute(
                        _tool.intent, params or {}
                    ).assembled_result()
                _, data = _tool.invoke(params or {})
                return data

            invoke.__name__ = tool.name
            result.append(StructuredTool.from_function(
                func=invoke,
                name=tool.name,
                description=tool.description,
                args_schema=ToolArgs,
            ))
        return result

    @classmethod
    def build_default(
        cls,
        client: AnalysisClient,
        *,
        default_limit: int = 100,
        max_limit: int = 1000,
    ) -> "ToolRegistry":
        adapter = ParameterAdapter(default_limit=default_limit, max_limit=max_limit)
        registry = cls()
        registry.register(MedicalTool(
            name="medical_aggregation",
            intent="aggregation_query",
            description="按医院、年龄、疾病、年份等维度计算医疗聚合指标",
            handler=client.run_aggregation,
            parameter_adapter=adapter.aggregation,
        ))
        algorithms = [
            ("statistics_overview", "medical_statistics", "statistics", adapter.statistics,
             "获取住院数据总体核心指标与关键分布"),
            ("association_analysis", "medical_association", "association", adapter.association,
             "挖掘疾病、操作和支付方式之间的关联规则"),
            ("cost_prediction", "medical_cost_prediction", "cost_prediction",
             adapter.cost_prediction, "训练费用模型或预测单次住院费用"),
            ("readmission_risk", "medical_readmission_risk", "readmission_risk",
             adapter.readmission_risk, "计算再入院风险画像或单条风险评分"),
        ]
        for intent, name, algorithm, transform, description in algorithms:
            registry.register(MedicalTool(
                name=name,
                intent=intent,
                description=description,
                handler=lambda params, algo=algorithm: client.run_algorithm(algo, params),
                parameter_adapter=transform,
            ))
        registry.register(MedicalTool(
            name="medical_metadata",
            intent="metadata_query",
            description="查询平台支持的维度、指标与算法元数据",
            handler=lambda params: client.metadata(params.get("kind")),
            parameter_adapter=adapter.metadata,
        ))
        return registry


class ToolExecutor:
    """按意图调用已注册工具，并只对瞬态失败执行指数退避重试。"""

    def __init__(
        self,
        registry: ToolRegistry,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.registry = registry
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleeper

    def execute(self, intent: str, params: dict[str, Any] | None = None) -> ToolCallResult:
        tool = self.registry.for_intent(intent)
        if tool is None:
            raise ToolParameterError(f"意图 {intent} 没有可调用的分析工具")

        max_attempts = max(1, int(self.retry_policy.max_attempts))
        started = time.perf_counter()
        normalized: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                normalized, raw = tool.invoke(params or {})
                elapsed = round(time.perf_counter() - started, 3)
                return ToolCallResult(
                    intent=intent,
                    tool_name=tool.name,
                    params=normalized,
                    raw_result=raw,
                    summary_data=normalize_for_summary(intent, raw),
                    attempts=attempt,
                    elapsed_seconds=elapsed,
                )
            except ToolParameterError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= max_attempts or not self._is_retryable(exc):
                    break
                self._sleep(self.retry_policy.delay_after(attempt))

        status_code = getattr(last_error, "status_code", None)
        trace_id = getattr(last_error, "trace_id", None)
        raise ToolInvocationError(
            "分析工具暂时无法完成调用，请稍后重试",
            tool_name=tool.name,
            attempts=attempt,
            status_code=status_code,
            trace_id=trace_id,
        ) from last_error

    def as_langchain_tools(self) -> list[Any]:
        """注册带参数转换、重试与结果组装语义的 LangChain 工具。"""
        return self.registry.as_langchain_tools(executor=self)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, AnalysisAPIError):
            return exc.retryable
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        code = getattr(exc, "code", None)
        try:
            return int(code) in {408, 429, 502, 503, 504}
        except (TypeError, ValueError):
            return False


def normalize_for_summary(intent: str, raw_result: Any) -> Any:
    """把人员3真实返回结构适配为人员4摘要模型期望的扁平事实结构。"""
    raw = copy.deepcopy(raw_result)
    business = raw
    if isinstance(raw, dict) and "result" in raw and "algorithm" in raw:
        business = raw.get("result")

    if not isinstance(business, dict):
        return business

    if intent == "aggregation_query":
        result: dict[str, Any] = {}
        if "dimensions" in business and business["dimensions"] is not None:
            labels = []
            for item in business["dimensions"]:
                if isinstance(item, dict):
                    labels.append(item.get("label") or item.get("key") or item.get("column"))
                else:
                    labels.append(item)
            result["dimensions"] = [item for item in labels if item]
        for key in ("metrics", "rows", "filters"):
            if key in business and business[key] is not None:
                result[key] = business[key]
        if "row_count" in business and business["row_count"] is not None:
            result["row_count"] = business["row_count"]
        elif isinstance(business.get("rows"), list):
            result["row_count"] = len(business["rows"])
        return result

    if intent == "statistics_overview":
        overview = business.get("overview") or {}
        result: dict[str, Any] = {}
        overview_fields = {
            "discharge_count": "total_discharges",
            "avg_length_of_stay": "avg_length_of_stay",
            "avg_total_charges": "avg_total_charges",
            "avg_total_costs": "avg_total_costs",
            "emergency_rate_pct": "emergency_rate",
        }
        for source_key, target_key in overview_fields.items():
            if source_key in overview and overview[source_key] is not None:
                result[target_key] = overview[source_key]
        for key in ("distributions", "top_diseases"):
            if key in business and business[key] is not None:
                result[key] = business[key]
        return result

    if intent == "association_analysis":
        if "rules" not in business or business["rules"] is None:
            return {
                "transaction_count": business["transaction_count"]
            } if business.get("transaction_count") is not None else {}
        rules = []
        for item in business["rules"]:
            rule = dict(item)
            left = next(iter((rule.get("antecedent") or {}).values()), "未知")
            right = next(iter((rule.get("consequent") or {}).values()), "未知")
            rule.setdefault("rule_str", f"{left} → {right}")
            rules.append(rule)
        result = {"rules": rules, "rule_count": len(rules)}
        if "transaction_count" in business and business["transaction_count"] is not None:
            result["transaction_count"] = business["transaction_count"]
        return result

    if intent == "cost_prediction":
        metrics = business.get("metrics") or {}
        mode = business.get("mode")
        result = {"mode": mode} if mode is not None else {}
        # train 与 predict 的事实字段互斥；不再用 0 伪装下游未返回的值。
        if mode in {None, "train"} and isinstance(metrics, dict):
            for key in ("mae", "rmse", "r2"):
                if key in metrics and metrics[key] is not None:
                    result[key] = metrics[key]
        if mode in {None, "predict"}:
            if (
                "predicted_total_charges" in business
                and business["predicted_total_charges"] is not None
            ):
                result["predicted_charge"] = business["predicted_total_charges"]
            for key in ("currency", "input"):
                if key in business and business[key] is not None:
                    result[key] = business[key]
        return result

    if intent == "readmission_risk":
        mode = business.get("mode")
        result = {"mode": mode} if mode is not None else {}
        if mode in {None, "profile"}:
            if "level_distribution" in business and business["level_distribution"] is not None:
                distribution = business["level_distribution"]
                result["level_distribution"] = distribution
                high = next(
                    (
                        item for item in distribution
                        if isinstance(item, dict) and item.get("level") == "High"
                    ),
                    None,
                )
                if high is not None and high.get("ratio") is not None:
                    result["high_risk_rate"] = high["ratio"]
            if (
                "high_risk_age_groups" in business
                and business["high_risk_age_groups"] is not None
            ):
                groups = business["high_risk_age_groups"]
                if (
                    groups
                    and isinstance(groups[0], dict)
                    and groups[0].get("age_group") is not None
                ):
                    result["top_risk_group"] = groups[0]["age_group"]
            if (
                "avg_risk_by_age_admission" in business
                and business["avg_risk_by_age_admission"] is not None
            ):
                result["profile"] = business["avg_risk_by_age_admission"]
        if mode in {None, "score"}:
            # 该算法是 0~100 的透明规则评分，不是统计概率。
            for key in ("risk_score", "risk_level", "contributions"):
                if key in business and business[key] is not None:
                    result[key] = business[key]
        return result

    return business
