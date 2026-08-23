# -*- coding: utf-8 -*-
"""人员1 AI 应用 API：聊天会话、历史恢复、工具元数据与洞察报告。"""

from __future__ import annotations

import hmac
import threading

from flask import Blueprint, request
from marshmallow import ValidationError
from werkzeug.exceptions import BadRequest

from app.application.service import MedicalAssistantService, build_application_service
from app.core.error_codes import ErrorCode
from app.core.exceptions import BizException, ServiceUnavailableError
from app.core.response import success
from app.extensions import ext
from app.schemas.application import ChatRequestSchema, ReportRequestSchema, ResourceIdSchema

bp = Blueprint("assistant", __name__, url_prefix="/api/v1/assistant")
_service_build_lock = threading.Lock()


@bp.before_request
def _authenticate_assistant_api():
    """保护包含医疗分析历史的应用层接口。

    开发/测试环境在未配置 key 时保持离线可用；生产环境采用
    fail-closed，避免因遗漏环境变量将会话和报告公开暴露。
    """
    settings = ext.settings
    expected = str(getattr(settings, "assistant_api_key", "") or "").strip()
    production = str(getattr(settings, "env", "development")).lower() == "production"
    if not expected:
        if production:
            raise ServiceUnavailableError("生产环境未配置 assistant API 凭证")
        return None

    supplied = str(request.headers.get("X-Assistant-API-Key") or "").strip()
    authorization = str(request.headers.get("Authorization") or "").strip()
    if not supplied and authorization:
        scheme, separator, credential = authorization.partition(" ")
        if separator and scheme.lower() == "bearer":
            supplied = credential.strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise BizException(ErrorCode.UNAUTHORIZED, "assistant API 凭证无效")
    return None


def _service() -> MedicalAssistantService:
    if ext.application_service is None:
        # 双重检查避免首次并发请求各自构建一套内存会话库，
        # 后完成的实例覆盖先完成实例并丢失刚刚创建的会话。
        with _service_build_lock:
            if ext.application_service is None:
                ext.application_service = build_application_service(
                    ext.settings,
                    data_provider=ext.data_provider,
                    cache=ext.cache,
                )
    return ext.application_service


def _json_object() -> dict:
    """严格解析 JSON 对象。

    特别是报告接口，非法 JSON 不能被 ``silent=True or {}`` 转成空参数，
    否则会意外生成整个会话的报告。
    """
    if not request.is_json:
        raise ValidationError({"_schema": ["请求体必须是 JSON 对象"]})
    try:
        payload = request.get_json(silent=False)
    except BadRequest as exc:
        raise ValidationError({"_schema": ["请求体不是有效 JSON"]}) from exc
    if not isinstance(payload, dict):
        raise ValidationError({"_schema": ["请求体必须是 JSON 对象"]})
    return payload


@bp.get("/health")
def health():
    return success(_service().health())


@bp.get("/tools")
def tools():
    return success(_service().tools_meta())


@bp.post("/chat")
def chat():
    payload = ChatRequestSchema().load(_json_object())
    result = _service().chat(
        payload["message"],
        session_id=payload.get("session_id"),
        analysis_id=payload.get("analysis_id"),
        generate_report=payload.get("generate_report", False),
        request_id=payload.get("request_id"),
    )
    return success(result.to_dict())


@bp.get("/sessions/<session_id>")
def get_session(session_id: str):
    session_id = ResourceIdSchema().load({"session_id": session_id})["session_id"]
    include_results = request.args.get("include_results", "false").lower() in {
        "1", "true", "yes"
    }
    return success(_service().get_session(session_id, include_results=include_results))


@bp.delete("/sessions/<session_id>")
def delete_session(session_id: str):
    session_id = ResourceIdSchema().load({"session_id": session_id})["session_id"]
    _service().delete_session(session_id)
    return success({"session_id": session_id, "deleted": True})


@bp.post("/sessions/<session_id>/reports")
def generate_report(session_id: str):
    session_id = ResourceIdSchema().load({"session_id": session_id})["session_id"]
    payload = ReportRequestSchema().load(_json_object())
    report = _service().generate_report(
        session_id,
        analysis_ids=payload.get("analysis_ids"),
        title=payload.get("title"),
    )
    return success(report)


@bp.get("/sessions/<session_id>/reports/<report_id>")
def get_report(session_id: str, report_id: str):
    ids = ResourceIdSchema().load({"session_id": session_id, "report_id": report_id})
    return success(_service().get_report(ids["session_id"], ids["report_id"]))
