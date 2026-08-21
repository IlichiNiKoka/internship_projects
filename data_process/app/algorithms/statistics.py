# -*- coding: utf-8 -*-
"""算法组件 2：统计指标计算（statistics）。

一次性输出平台核心统计指标与关键分布，作为大屏概览与
AI 智能交互模块“总体情况”类问题的统一数据源。
"""

from __future__ import annotations

from typing import Any

from pyspark.sql import functions as F

from app.algorithms.base import Algorithm, AlgorithmContext, ParamSpec, register_algorithm

# 分布类指标：维度 key -> (中文名, DataFrame 列)
_DISTRIBUTION_DIMS = [
    ("age_group", "年龄组"),
    ("gender", "性别"),
    ("type_of_admission", "入院类型"),
    ("payment_typology_1", "支付方式"),
    ("ccsr_diagnosis_description", "疾病类型"),
    ("hospital_county", "所在县"),
]


@register_algorithm
class StatisticsAlgorithm(Algorithm):
    name = "statistics"
    display_name = "统计指标计算"
    version = "1.0.0"
    description = (
        "计算平台核心统计指标：总人次、平均住院时长、平均费用/成本、急诊率、"
        "平均病情严重度，以及年龄/性别/入院类型/支付/疾病/县区等分布。"
    )
    tags = ("statistics", "dashboard", "overview")

    param_specs = [
        ParamSpec("top_n", "int", required=False, default=10,
                  min_value=1, max_value=50, description="各分布返回的头部条目数"),
    ]

    def _execute(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        df = ctx.dataframe
        top_n = ctx.params.get("top_n", 10)

        # ---- 总体指标（单次扫描）----
        overview_row = df.agg(
            F.count("*").alias("discharge_count"),
            F.round(F.avg("length_of_stay"), 2).alias("avg_length_of_stay"),
            F.round(F.avg("total_charges"), 2).alias("avg_total_charges"),
            F.round(F.avg("total_costs"), 2).alias("avg_total_costs"),
            F.round(F.sum("total_charges"), 2).alias("sum_total_charges"),
            F.round(F.sum("total_costs"), 2).alias("sum_total_costs"),
            F.round(F.avg("apr_severity_of_illness_code"), 2).alias("avg_severity_of_illness"),
            F.round(F.avg(F.when(F.col("emergency_department_indicator") == "Y", 1.0)
                          .otherwise(0.0)) * 100, 2).alias("emergency_rate_pct"),
        ).first().asDict()

        # ---- 分布指标 ----
        distributions: dict[str, list[dict]] = {}
        for dim_key, label_cn in _DISTRIBUTION_DIMS:
            dim = df.groupBy(dim_key).agg(F.count("*").alias("discharge_count"))
            rows = (
                dim.orderBy(F.col("discharge_count").desc(), F.col(dim_key).asc())
                .limit(top_n)
                .collect()
            )
            distributions[dim_key] = [
                {"value": r[dim_key], "count": r["discharge_count"]} for r in rows
            ]

        # ---- Top 疾病（按人次）----
        top_diseases = (
            df.groupBy("ccsr_diagnosis_description")
            .agg(
                F.count("*").alias("discharge_count"),
                F.round(F.avg("total_charges"), 2).alias("avg_total_charges"),
            )
            .orderBy(F.col("discharge_count").desc())
            .limit(top_n)
            .collect()
        )
        top_diseases = [
            {"disease": r["ccsr_diagnosis_description"],
             "discharge_count": r["discharge_count"],
             "avg_total_charges": r["avg_total_charges"]}
            for r in top_diseases
        ]

        result = {
            "overview": overview_row,
            "distributions": distributions,
            "top_diseases": top_diseases,
        }
        return result, {"rows_scanned": int(overview_row["discharge_count"])}, "统计指标计算完成"
