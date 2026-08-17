# -*- coding: utf-8 -*-
"""AI 智能层请求 Schema（人员4：意图识别 / 文本生成）。

只做结构校验；意图/参数合法性在 AIService / IntentClassifier 内完成。
"""
from __future__ import annotations

from marshmallow import Schema, fields, validate as ma_validate


class IntentRequestSchema(Schema):
    """POST /api/v1/ai/intent 请求体。"""
    query = fields.Str(required=True,
                       validate=ma_validate.Length(min=1, max=500),
                       error_messages={"required": "缺少 query 自然语言输入"})


class SummaryRequestSchema(Schema):
    """POST /api/v1/ai/summary 请求体。

    用户已有分析结果，直接调用文本生成。
    """
    query = fields.Str(required=True,
                       validate=ma_validate.Length(min=1, max=500),
                       error_messages={"required": "缺少 query 用户原始查询"})
    intent_key = fields.Str(required=True,
                            validate=ma_validate.OneOf([
                                "aggregation_query",
                                "statistics_overview",
                                "association_analysis",
                                "cost_prediction",
                                "readmission_risk",
                                "metadata_query",
                                "unsupported",
                            ]),
                            error_messages={"required": "缺少 intent_key"})
    intent_label = fields.Str(load_default="")
    analysis_result = fields.Raw(required=True,
                                 error_messages={"required":
                                                 "缺少 analysis_result"})


class ExecuteRequestSchema(Schema):
    """POST /api/v1/ai/execute 请求体：端到端执行。"""
    query = fields.Str(required=True,
                       validate=ma_validate.Length(min=1, max=500),
                       error_messages={"required": "缺少 query 自然语言输入"})
