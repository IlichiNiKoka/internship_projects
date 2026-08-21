# -*- coding: utf-8 -*-
"""中间件（需求 3.3.3）：trace_id 生成与透传、请求计时、统一异常处理。

职责：
  1. before_request：生成/透传 trace_id（X-Request-ID），记录起始时间与访问日志；
  2. after_request：响应头回写 X-Request-ID / X-Query-Time，兜底包装未标准化的响应；
  3. errorhandler：把 BizException、marshmallow 校验错误、werkzeug HTTP 异常、
     未预期异常统一转换为标准 JSON（code/message/data/query_time/trace_id）。
"""

from __future__ import annotations

import logging
import time
import uuid

from flask import Flask, g, has_request_context, jsonify, request
from marshmallow import ValidationError
from werkzeug.exceptions import HTTPException

from app.core.error_codes import ErrorCode, default_message
from app.core.exceptions import BizException
from app.core.response import build

logger = logging.getLogger(__name__)

# 请求体过大限制（防大 JSON 压垮服务）
_MAX_CONTENT_LENGTH = 2 * 1024 * 1024


def _is_standard_body(body) -> bool:
    """判断响应体是否已是标准结构（有 code/message/data 三键）。"""
    return (
        isinstance(body, dict)
        and "code" in body
        and "message" in body
        and "data" in body
    )


def register_middlewares(app: Flask) -> None:
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH

    @app.before_request
    def _before():
        g.trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        g._request_start = time.perf_counter()
        if request.path.startswith("/api/"):
            logger.info("-> %s %s", request.method, request.full_path.rstrip("?"))

    @app.after_request
    def _after(response):
        elapsed = None
        # 1) 响应头回写追踪信息
        response.headers["X-Request-ID"] = getattr(g, "trace_id", "-")
        if hasattr(g, "_request_start"):
            elapsed = time.perf_counter() - g._request_start
            response.headers["X-Query-Time"] = f"{elapsed:.3f}"
        # 2) 兜底：视图直接 return dict/list 时自动包装为标准成功响应
        if response.is_json and not _is_standard_body(response.get_json(silent=True)):
            data = response.get_json()
            body = build(ErrorCode.OK, "OK", data)
            body["query_time"] = elapsed if elapsed is not None else 0.0
            body["trace_id"] = getattr(g, "trace_id", body["trace_id"])
            response.set_data(jsonify(body).get_data())
        return response

    # ---- 统一异常处理（按错误码表组装响应）----
    @app.errorhandler(BizException)
    def _handle_biz(e: BizException):
        logger.warning("业务异常 code=%s message=%s detail=%s",
                       int(e.code), e.message, e.detail)
        return jsonify(build(e.code, e.message, None if e.detail is None else {"detail": e.detail})), int(e.code)

    @app.errorhandler(ValidationError)
    def _handle_validation(e: ValidationError):
        logger.warning("参数校验失败: %s", e.messages)
        body = build(ErrorCode.PARAM_VALIDATION_ERROR, "参数校验失败", {"detail": e.messages})
        return jsonify(body), int(ErrorCode.PARAM_VALIDATION_ERROR)

    @app.errorhandler(HTTPException)
    def _handle_http(e: HTTPException):
        # 404/405/413 等 Flask 框架异常，映射到统一错误码
        code = e.code if e.code in (400, 404, 405, 408, 413, 415, 429) else 500
        logger.warning("HTTP 异常 %s %s", code, e.description)
        body = build(ErrorCode(code), e.description or default_message(code))
        return jsonify(body), code

    @app.errorhandler(Exception)
    def _handle_unexpected(e: Exception):
        logger.exception("未预期异常: %s", e)
        if has_request_context():
            g.trace_id = getattr(g, "trace_id", "-")
        body = build(ErrorCode.INTERNAL_ERROR, default_message(ErrorCode.INTERNAL_ERROR))
        return jsonify(body), int(ErrorCode.INTERNAL_ERROR)
