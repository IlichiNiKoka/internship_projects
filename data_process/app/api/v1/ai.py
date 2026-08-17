# -*- coding: utf-8 -*-
"""AI 智能交互 API（人员4 需求 3.X.1 / 3.X.2 / 3.X.4）。

接口清单：
  POST /api/v1/ai/intent     意图识别（仅识别，不调用下游）
  POST /api/v1/ai/summary    分析结果文本生成（已有 analysis_result）
  POST /api/v1/ai/execute    端到端（意图识别 + 下游调用 + 文本生成）
  GET  /api/v1/ai/meta       AI 能力元数据（意图清单/数据集规模/LLM 配置）
  GET  /api/v1/ai/health     AI 子系统健康检查
"""
from __future__ import annotations

from flask import Blueprint, request

from app.ai.service import AIService, get_ai_service
from app.core.response import success
from app.extensions import ext
from app.schemas.ai import (
    ExecuteRequestSchema,
    IntentRequestSchema,
    SummaryRequestSchema,
)

bp = Blueprint("ai", __name__, url_prefix="/api/v1/ai")


# ---------------------------------------------------------------------------
def _service() -> AIService:
    """获取 AI 服务实例（首次访问时按 ext.settings 注入下游依赖）。"""
    svc = get_ai_service(ext.settings)
    # 注入下游服务（首次或 settings 变更时）
    if svc._aggregation is None:
        from app.services.aggregation_service import AggregationService
        svc._aggregation = AggregationService(
            provider=ext.data_provider,
            cache=ext.cache,
            settings=ext.settings,
        )
    if svc._algorithm is None:
        from app.services.algorithm_service import AlgorithmService
        svc._algorithm = AlgorithmService(provider=ext.data_provider)
    return svc


# ---------------------------------------------------------------------------
@bp.get("/health")
def health():
    """AI 子系统健康检查。"""
    svc = _service()
    return success({
        "status": "ok",
        "llm_provider": svc._client.provider,
        "model": ext.settings.llm_model,
        "api_key_configured": bool(ext.settings.llm_api_key),
    })


@bp.get("/meta")
def meta():
    """AI 能力元数据。"""
    return success(_service().meta())


@bp.post("/intent")
def recognize_intent():
    """意图识别：用户自然语言 -> 标准化意图 + 参数。"""
    payload = IntentRequestSchema().load(request.get_json(silent=True) or {})
    result = _service().recognize_intent(payload["query"])
    return success(result.to_dict())


@bp.post("/summary")
def generate_summary():
    """分析结果文本生成：已有 analysis_result 时调用。"""
    payload = SummaryRequestSchema().load(request.get_json(silent=True) or {})
    result = _service().generate_summary(
        query=payload["query"],
        intent_label=payload.get("intent_label") or payload["intent_key"],
        intent_key=payload["intent_key"],
        analysis_result=payload["analysis_result"],
    )
    return success(result.to_dict())


@bp.post("/execute")
def execute():
    """端到端：意图识别 -> 下游调用 -> 文本生成。"""
    payload = ExecuteRequestSchema().load(request.get_json(silent=True) or {})
    result = _service().execute(payload["query"])
    return success(result.to_dict())
