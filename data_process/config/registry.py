# -*- coding: utf-8 -*-
"""维度/指标注册表：多维度聚合分析的口径白名单。

为什么需要注册表？
  1. **防注入**：维度、指标、过滤字段只能来自本表，杜绝拼接任意列名/SQL；
  2. **口径统一**：所有接口、算法共用同一份中文名/单位/精度，前后端一致；
  3. **可扩展**：新增维度或指标只需在本文件加一行，接口与元数据自动生效。

字段命名遵循清洗后数据字典（data_dictionary.md，小写下划线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from pyspark.sql import Column, functions as F

# ---------------------------------------------------------------------------
# 维度注册表
# ---------------------------------------------------------------------------
# value_type: string 只允许 eq/ne/in/not_in 过滤；integer/double 额外支持范围过滤
@dataclass(frozen=True)
class DimensionSpec:
    key: str            # API 中使用的中文语义键
    column: str         # 数据表中的实际列名
    label_cn: str       # 中文名（供 AI 智能交互模块与大屏展示）
    value_type: str = "string"   # string / integer / double
    description: str = ""


DIMENSIONS: list[DimensionSpec] = [
    # ---- 时间 ----
    DimensionSpec("discharge_year", "discharge_year", "出院年份", "integer", "数据年份（2021）"),
    # ---- 患者画像 ----
    DimensionSpec("age_group", "age_group", "年龄组", "string", "0 to 17 / 18 to 29 / 30 to 49 / 50 to 69 / 70 or Older"),
    DimensionSpec("gender", "gender", "性别", "string", "Male / Female / Unknown"),
    DimensionSpec("race", "race", "种族", "string", "White / Other Race / Black/African American / Multi-racial"),
    DimensionSpec("ethnicity", "ethnicity", "族裔", "string", "Spanish/Hispanic / Not Span/Hispanic / ..."),
    DimensionSpec("zip_code_3_digits", "zip_code_3_digits", "邮编前缀", "string", "3 位邮编前缀，OOS=州外"),
    # ---- 机构/地域 ----
    DimensionSpec("hospital_service_area", "hospital_service_area", "医院服务区域", "string", "如 New York City"),
    DimensionSpec("hospital_county", "hospital_county", "所在县", "string", "如 Bronx / Kings"),
    DimensionSpec("facility_name", "facility_name", "医院", "string", "医院全称"),
    # ---- 疾病/诊疗 ----
    DimensionSpec("ccsr_diagnosis_code", "ccsr_diagnosis_code", "疾病编码", "string", "CCSR 诊断编码，如 INF012"),
    DimensionSpec("ccsr_diagnosis_description", "ccsr_diagnosis_description", "疾病类型", "string", "CCSR 诊断描述，如 CORONAVIRUS DISEASE 2019"),
    DimensionSpec("ccsr_procedure_description", "ccsr_procedure_description", "操作类型", "string", "CCSR 操作描述"),
    DimensionSpec("apr_drg_description", "apr_drg_description", "DRG病组", "string", "APR DRG 描述"),
    DimensionSpec("apr_mdc_description", "apr_mdc_description", "MDC大类", "string", "APR MDC 描述"),
    DimensionSpec("apr_severity_of_illness_description", "apr_severity_of_illness_description", "病情严重程度", "string", "Minor / Moderate / Major / Extreme"),
    DimensionSpec("apr_risk_of_mortality", "apr_risk_of_mortality", "死亡风险", "string", "Minor / Moderate / Major / Extreme（文本字段）"),
    DimensionSpec("apr_medical_surgical_description", "apr_medical_surgical_description", "内外科标志", "string", "Medical / Surgical / Not Applicable"),
    # ---- 住院过程 ----
    DimensionSpec("type_of_admission", "type_of_admission", "入院类型", "string", "Emergency / Elective / Newborn / Urgent / Trauma"),
    DimensionSpec("patient_disposition", "patient_disposition", "出院去向", "string", "Home or Self Care / Expired / ..."),
    DimensionSpec("emergency_department_indicator", "emergency_department_indicator", "急诊标志", "string", "Y / N"),
    # ---- 支付 ----
    DimensionSpec("payment_typology_1", "payment_typology_1", "支付方式(主)", "string", "Medicare / Medicaid / Private / ..."),
    DimensionSpec("payment_typology_2", "payment_typology_2", "支付方式(次)", "string", "多数为空"),
    DimensionSpec("payment_typology_3", "payment_typology_3", "支付方式(三)", "string", "多数为空"),
]

DIMENSION_BY_KEY: dict[str, DimensionSpec] = {d.key: d for d in DIMENSIONS}
DIMENSION_BY_COLUMN: dict[str, DimensionSpec] = {d.column: d for d in DIMENSIONS}


# ---------------------------------------------------------------------------
# 指标注册表
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricSpec:
    key: str                 # API 中使用的中文语义键
    label_cn: str            # 中文名
    unit: str = ""           # 单位
    decimals: int = 2        # 输出数值保留小数位
    # 聚合方式与源列（count 时 column 为空）
    agg: str = "avg"         # count / avg / sum / max
    column: str | None = None
    description: str = ""

    @property
    def output_name(self) -> str:
        """输出到 JSON 的字段名（与 key 一致）。"""
        return self.key

    def agg_expr(self) -> Column:
        """生成 Spark 聚合表达式（列名统一别名为 key）。"""
        if self.agg == "count":
            expr = F.count("*")
        elif self.agg == "avg":
            expr = F.avg(F.col(self.column))
        elif self.agg == "sum":
            expr = F.sum(F.col(self.column))
        elif self.agg == "max":
            expr = F.max(F.col(self.column))
        else:  # pragma: no cover - 注册表内不会出现
            raise ValueError(f"未知聚合方式: {self.agg}")
        return expr.alias(self.output_name)


METRICS: list[MetricSpec] = [
    MetricSpec("discharge_count", "住院人次", "人次", 0, "count", None,
               "COUNT(*) 住院记录数"),
    MetricSpec("avg_length_of_stay", "平均住院时长", "天", 1, "avg", "length_of_stay",
               "AVG(Length of Stay)"),
    MetricSpec("max_length_of_stay", "最长住院时长", "天", 0, "max", "length_of_stay",
               "MAX(Length of Stay)"),
    MetricSpec("avg_total_charges", "平均总费用", "美元", 2, "avg", "total_charges",
               "AVG(Total Charges)"),
    MetricSpec("sum_total_charges", "总费用合计", "美元", 2, "sum", "total_charges",
               "SUM(Total Charges)"),
    MetricSpec("avg_total_costs", "平均总成本", "美元", 2, "avg", "total_costs",
               "AVG(Total Costs)"),
    MetricSpec("sum_total_costs", "总成本合计", "美元", 2, "sum", "total_costs",
               "SUM(Total Costs)"),
    MetricSpec("avg_birth_weight", "平均出生体重", "克", 0, "avg", "birth_weight",
               "AVG(Birth Weight)，非新生儿记录为空不参与"),
    MetricSpec("avg_severity_of_illness", "平均病情严重度", "级", 2, "avg",
               "apr_severity_of_illness_code", "AVG(APR Severity of Illness Code) 0~4"),
]

METRIC_BY_KEY: dict[str, MetricSpec] = {m.key: m for m in METRICS}

# 排序时允许的字段 = 维度列 + 指标输出名
SORTABLE_FIELDS: set[str] = {d.column for d in DIMENSIONS} | {m.key for m in METRICS}

# ---------------------------------------------------------------------------
# 过滤操作符白名单
# ---------------------------------------------------------------------------
# 文本字段可用：eq / ne / in / not_in
# 数值字段追加：gte / gt / lte / lt / between
FILTER_OPS: set[str] = {"eq", "ne", "in", "not_in", "gte", "gt", "lte", "lt", "between"}
STRING_FILTER_OPS: set[str] = {"eq", "ne", "in", "not_in"}
NUMERIC_FILTER_OPS: set[str] = FILTER_OPS

# 允许作为过滤条件但不在维度注册表中的数值列（如住院时长、费用、体重、严重度代码）
NUMERIC_FILTER_COLUMNS: set[str] = {
    "length_of_stay",
    "total_charges",
    "total_costs",
    "birth_weight",
    "apr_severity_of_illness_code",
}


def resolve_dimension(key_or_column: str) -> DimensionSpec | None:
    """支持按 key 或真实列名解析维度（AI 交互模块可能传入两者之一）。"""
    return DIMENSION_BY_KEY.get(key_or_column) or DIMENSION_BY_COLUMN.get(key_or_column)


def resolve_metric(key: str) -> MetricSpec | None:
    return METRIC_BY_KEY.get(key)


def dimension_meta() -> list[dict]:
    return [
        {"key": d.key, "column": d.column, "label": d.label_cn,
         "value_type": d.value_type, "description": d.description}
        for d in DIMENSIONS
    ]


def metric_meta() -> list[dict]:
    return [
        {"key": m.key, "label": m.label_cn, "unit": m.unit,
         "aggregation": m.agg, "column": m.column,
         "description": m.description}
        for m in METRICS
    ]
