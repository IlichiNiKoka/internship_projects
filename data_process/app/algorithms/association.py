# -*- coding: utf-8 -*-
"""算法组件 3：疾病-操作关联分析（association）。

基于每次住院记录同时包含「诊断（CCSR Diagnosis）」与「操作（CCSR Procedure）」
两个字段的数据特征，挖掘“什么诊断常见伴生什么操作”的关联规则：
    support(A∧B) = N_AB / N
    confidence(A→B) = N_AB / N_A
    lift(A→B) = confidence(A→B) / support(B)

输出 Top-N 规则，支撑临床路径分析与 AI 问答（“肺炎患者常做哪些操作？”）。
"""

from __future__ import annotations

from typing import Any

from pyspark.sql import functions as F

from app.algorithms.base import Algorithm, AlgorithmContext, ParamSpec, register_algorithm
from app.core.exceptions import ParamValidationError

# 允许作为前件/后件的字段（注册表白名单，防任意列注入）
_ALLOWED_FIELDS = {
    "ccsr_diagnosis_description": "疾病类型",
    "ccsr_diagnosis_code": "疾病编码",
    "ccsr_procedure_description": "操作类型",
    "ccsr_procedure_code": "操作编码",
    "apr_mdc_description": "MDC大类",
    "apr_severity_of_illness_description": "病情严重程度",
    "type_of_admission": "入院类型",
    "payment_typology_1": "支付方式",
    "age_group": "年龄组",
}

_UNKNOWN = "Unknown"


@register_algorithm
class AssociationAlgorithm(Algorithm):
    name = "association"
    display_name = "疾病关联分析"
    version = "1.0.0"
    description = (
        "挖掘诊断-操作（或任意两个白名单字段）间的关联规则，"
        "输出支持度/置信度/提升度 Top-N 规则。"
    )
    tags = ("mining", "association-rules", "apriori-lite")

    param_specs = [
        ParamSpec("antecedent", "str", required=False, default="ccsr_diagnosis_description",
                  allowed_values=tuple(_ALLOWED_FIELDS), description="规则前件字段（A）"),
        ParamSpec("consequent", "str", required=False, default="ccsr_procedure_description",
                  allowed_values=tuple(_ALLOWED_FIELDS), description="规则后件字段（B）"),
        ParamSpec("min_support", "float", required=False, default=0.005,
                  min_value=1e-6, max_value=1.0, description="最小支持度（规则过滤阈值）"),
        ParamSpec("top_n", "int", required=False, default=20,
                  min_value=1, max_value=200, description="返回规则条数"),
    ]

    def validate(self, params: dict) -> dict:
        """统一参数校验（备选流A）：前件与后件字段不能相同。"""
        validated = super().validate(params)
        if validated["antecedent"] == validated["consequent"]:
            raise ParamValidationError(
                detail={"antecedent": validated["antecedent"],
                        "consequent": validated["consequent"]},
                message="前件与后件字段不能相同",
            )
        return validated

    def _execute(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        df = ctx.dataframe
        a_field = ctx.params["antecedent"]
        b_field = ctx.params["consequent"]
        min_support = ctx.params["min_support"]
        top_n = ctx.params["top_n"]

        if a_field == b_field:
            raise ParamValidationError(
                detail={"antecedent": a_field, "consequent": b_field},
                message="前件与后件字段不能相同",
            )

        # 过滤缺失哨兵（Unknown 不参与规则挖掘，否则会挖出大量无意义规则）
        base = df.filter(
            (F.col(a_field) != _UNKNOWN) & (F.col(b_field) != _UNKNOWN)
        ).select(a_field, b_field)

        # N_AB（共现计数）
        pair_counts = (
            base.groupBy(a_field, b_field)
            .agg(F.count("*").alias("n_ab"))
        )
        # N_A、N_B（单项计数）
        count_a = base.groupBy(a_field).agg(F.count("*").alias("n_a"))
        count_b = base.groupBy(b_field).agg(F.count("*").alias("n_b"))
        total = base.count()

        # 关联规则计算（join 传播计数，单次 shuffle 内完成）
        rules = (
            pair_counts
            .join(count_a, on=a_field)
            .join(count_b, on=b_field)
            .withColumn("support_ab", F.col("n_ab") / F.lit(total))
            .withColumn("support_a", F.col("n_a") / F.lit(total))
            .withColumn("support_b", F.col("n_b") / F.lit(total))
            .withColumn("confidence", F.col("n_ab") / F.col("n_a"))
            .withColumn("lift", F.col("confidence") / F.col("support_b"))
            .filter(F.col("support_ab") >= F.lit(min_support))
            .select(
                a_field, b_field,
                F.round("support_ab", 6).alias("support"),
                F.round("confidence", 4).alias("confidence"),
                F.round("lift", 3).alias("lift"),
                "n_ab",
            )
            .orderBy(F.col("support").desc(), F.col("lift").desc())
            .limit(top_n)
        )

        rule_rows = [
            {
                "antecedent": {a_field: r[a_field]},
                "consequent": {b_field: r[b_field]},
                "support": r["support"],
                "confidence": r["confidence"],
                "lift": r["lift"],
                "cooccurrence_count": r["n_ab"],
            }
            for r in rules.collect()
        ]

        result = {
            "antecedent_field": a_field,
            "consequent_field": b_field,
            "min_support": min_support,
            "transaction_count": total,
            "rules": rule_rows,
        }
        metrics = {"transaction_count": total, "rule_count": len(rule_rows)}
        return result, metrics, "关联规则挖掘完成"
