# -*- coding: utf-8 -*-
"""算法组件 5：患者再入院风险评分（readmission_risk）。

重要说明（数据口径）：
  SPARCS 数据集为脱敏数据，**不含患者唯一标识**，无法按患者历史计算
  真实的“30 天再入院率”。因此一期采用**高风险入院特征评分**作为代理实现：
  基于与再入院强相关的住院特征（高龄、急诊入院、病情严重、死亡风险高、
  长住院、非居家出院）做加权评分，输出 0~100 风险分与 Low/Medium/High 等级。

  二期接入含患者主键的入院流水后，可将本算法升级为真实的再入院预测模型
  （接口与参数保持兼容）。

两种模式：
  * profile：人群画像 —— 各年龄组 × 入院类型的平均风险分与等级分布；
  * score  ：单条评估 —— 对给定特征向量输出风险分、等级与各维度贡献。
"""

from __future__ import annotations

import math
from typing import Any

from pyspark.sql import functions as F

from app.algorithms.base import Algorithm, AlgorithmContext, ParamSpec, register_algorithm
from app.core.exceptions import ParamValidationError

# 评分规则：维度 -> 条件 -> 分值（规则引擎，透明可审计）
_SCORE_RULES: list[dict] = [
    {"name": "高龄", "field": "age_group",
     "map": {"70 or Older": 25, "50 to 69": 10}},
    {"name": "急诊入院", "field": "type_of_admission", "map": {"Emergency": 20}},
    {"name": "病情严重", "field": "apr_severity_of_illness_description",
     "map": {"Extreme": 25, "Major": 15, "Moderate": 5}},
    {"name": "死亡风险高", "field": "apr_risk_of_mortality",
     "map": {"Extreme": 15, "Major": 10}},
    {"name": "住院超10天", "field": "length_of_stay", "threshold": 10, "points": 10},
    {"name": "非居家出院", "field": "patient_disposition",
     "exclude": ["Home or Self Care"], "points": 5},
]

# 等级阈值
_HIGH_THRESHOLD = 60.0
_MEDIUM_THRESHOLD = 30.0


def _score_expr():
    """生成 Spark 评分表达式（对全量数据并行评分，供 profile 模式使用）。"""
    expr = F.lit(0)
    for rule in _SCORE_RULES:
        field, name = rule["field"], rule["name"]
        if "map" in rule:
            case = None
            for value, points in rule["map"].items():
                when = F.when(F.col(field) == value, points)
                case = when if case is None else case.when(F.col(field) == value, points)
            expr = expr + case.otherwise(0)
        elif "threshold" in rule:
            expr = expr + F.when(F.col(field) >= rule["threshold"], rule["points"]).otherwise(0)
        elif "exclude" in rule:
            expr = expr + F.when(~F.col(field).isin(rule["exclude"]), rule["points"]).otherwise(0)
    return expr.alias("risk_score")


def _score_one(features: dict) -> tuple[float, list[dict]]:
    """单条记录评分（score 模式），返回 (总分, 维度贡献)。"""
    total = 0.0
    contributions = []
    for rule in _SCORE_RULES:
        value = features.get(rule["field"])
        points = 0.0
        if "map" in rule:
            points = float(rule["map"].get(str(value), 0))
        elif "threshold" in rule and isinstance(value, (int, float)):
            points = float(rule["points"]) if value >= rule["threshold"] else 0.0
        elif "exclude" in rule:
            if value is None or str(value) not in rule["exclude"]:
                points = float(rule["points"])
        total += points
        contributions.append({"factor": rule["name"], "field": rule["field"],
                              "value": value, "points": points})
    return round(total, 1), contributions


def _risk_level(score: float) -> str:
    if score >= _HIGH_THRESHOLD:
        return "High"
    if score >= _MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"


@register_algorithm
class ReadmissionRiskAlgorithm(Algorithm):
    name = "readmission_risk"
    display_name = "再入院风险评分"
    version = "1.0.0"
    description = (
        "基于高龄/急诊/病情严重度/死亡风险/长住院等特征的风险规则评分，"
        "输出 0~100 风险分与等级（脱敏数据无患者ID，一期为代理实现）。"
    )
    tags = ("risk", "rule-engine", "scoring")

    param_specs = [
        ParamSpec("mode", "str", required=False, default="profile",
                  allowed_values=("profile", "score"),
                  description="profile=人群画像；score=单条评估"),
        ParamSpec("sample", "dict", required=False,
                  description="score 模式必填：单条住院特征（length_of_stay 为数值，其余为文本）"),
    ]

    def _execute(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        mode = ctx.params["mode"]
        if mode == "score":
            return self._score(ctx)
        return self._profile(ctx)

    def _profile(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        df = ctx.dataframe.withColumn("risk_score", _score_expr())
        df = df.withColumn(
            "risk_level",
            F.when(F.col("risk_score") >= _HIGH_THRESHOLD, F.lit("High"))
            .when(F.col("risk_score") >= _MEDIUM_THRESHOLD, F.lit("Medium"))
            .otherwise(F.lit("Low")),
        )

        # 1) 总体等级分布
        level_dist = (
            df.groupBy("risk_level").count()
            .orderBy("risk_level").collect()
        )
        # 2) 年龄组 × 入院类型 平均风险
        profile = (
            df.groupBy("age_group", "type_of_admission")
            .agg(
                F.count("*").alias("discharge_count"),
                F.round(F.avg("risk_score"), 1).alias("avg_risk_score"),
            )
            .orderBy(F.col("avg_risk_score").desc())
            .limit(50)
            .collect()
        )
        # 3) 高风险人群特征画像（Top 高风险年龄组）
        high_risk_age = (
            df.filter(F.col("risk_level") == "High")
            .groupBy("age_group").count()
            .orderBy(F.col("count").desc())
            .limit(10).collect()
        )

        total = df.count()
        result = {
            "mode": "profile",
            "scoring_rules": _SCORE_RULES,
            "level_thresholds": {"medium": _MEDIUM_THRESHOLD, "high": _HIGH_THRESHOLD},
            "level_distribution": [
                {"level": r["risk_level"], "count": r["count"],
                 "ratio": round(r["count"] / total, 4)}
                for r in level_dist
            ],
            "avg_risk_by_age_admission": [
                {"age_group": r["age_group"], "type_of_admission": r["type_of_admission"],
                 "discharge_count": r["discharge_count"],
                 "avg_risk_score": r["avg_risk_score"]}
                for r in profile
            ],
            "high_risk_age_groups": [
                {"age_group": r["age_group"], "count": r["count"]} for r in high_risk_age
            ],
        }
        return result, {"rows_scored": total}, "再入院风险画像计算完成"

    def _score(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        sample = ctx.params.get("sample")
        if not sample:
            raise ParamValidationError(detail={"sample": "score 模式必须提供 sample 参数"},
                                       message="score 模式必须提供 sample 参数")
        score, contributions = _score_one(sample)
        level = _risk_level(score)
        result = {
            "mode": "score",
            "risk_score": score,
            "risk_level": level,
            "contributions": contributions,
        }
        return result, None, "再入院风险评分完成"
