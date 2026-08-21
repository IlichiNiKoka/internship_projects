# -*- coding: utf-8 -*-
"""中间件（需求 3.3.3 + 3.3.4 + 3.3.5）。

职责：
  1. before_request：生成/透传 trace_id（X-Request-ID），记录起始时间与访问日志；
     二期新增：调用方权限控制（401/403）与滑动窗口限流（429）；
  2. after_request：响应头回写 X-Request-ID / X-Query-Time，兜底包装未标准化的响应；
     二期新增：接口耗时监控与慢查询告警（3.3.4）；
  3. errorhandler：把 BizException、marshmallow 校验错误、werkzeug HTTP 异常、
     未预期异常统一转换为标准 JSON（code/message/data/query_time/trace_id）；
     二期新增：错误计数入监控、5xx 告警日志、429 回写 Retry-After 头。
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid

from flask import Flask, g, has_request_context, jsonify, request
from marshmallow import ValidationError
from werkzeug.exceptions import HTTPException

from app.core.error_codes import ErrorCode, default_message
from app.core.exceptions import (
    BizException,
    ForbiddenError,
    RateLimitError,
    UnauthorizedError,
)
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


def _extract_token() -> str:
    """从 Authorization: Bearer <token> 或 X-API-Key 提取认证凭证。"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return (request.headers.get("X-API-Key") or "").strip()


def _is_public_path(path: str, settings) -> bool:
    """判断路径是否在免认证/免限流白名单（如健康检查）。"""
    public = getattr(settings, "api_auth_public_paths", "") or ""
    for prefix in (p.strip().rstrip("/") for p in public.split(",") if p.strip()):
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _record_error(code: int) -> None:
    """把错误计入性能监控（需求 3.3.5：错误日志记录与告警）。"""
    from app.extensions import ext
    monitor = getattr(ext, "monitor", None)
    if monitor is not None:
        monitor.record_error(int(code))


def register_middlewares(app: Flask) -> None:
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH

    @app.before_request
    def _before():
        g.trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        g._request_start = time.perf_counter()
        if request.path.startswith("/api/"):
            logger.info("-> %s %s", request.method, request.full_path.rstrip("?"))

    @app.before_request
    def _guard():
        """权限控制与限流（需求 3.3.5）：对 /api/ 接口统一守卫。"""
        from app.extensions import ext
        settings = ext.settings
        if settings is None or not request.path.startswith("/api/"):
            return
        if _is_public_path(request.path, settings):
            return

        # 1) 调用方权限控制：校验 API Token（缺 -> 401，错 -> 403）
        if settings.api_auth_enabled:
            token = _extract_token()
            if not token:
                raise UnauthorizedError(
                    detail={"hint": "缺少认证凭证：请携带 Authorization: Bearer <token> 或 X-API-Key 头"},
                )
            valid = [t.strip() for t in (settings.api_auth_tokens or "").split(",") if t.strip()]
            if valid and any(secrets.compare_digest(token, t) for t in valid):
                g.api_token = token
            else:
                raise ForbiddenError(
                    detail={"hint": "认证凭证无效或无权限访问该资源"},
                )

        # 2) 限流：按客户端标识（Token 或来源 IP）滑动窗口计数，超限 -> 429
        if settings.rate_limit_enabled:
            limiter = getattr(ext, "rate_limiter", None)
            if limiter is not None:
                identity = (
                    getattr(g, "api_token", None)
                    or request.headers.get("X-API-Key")
                    or request.remote_addr or "unknown"
                )
                allowed, retry_after = limiter.allow(
                    identity, settings.rate_limit_requests, settings.rate_limit_window_seconds,
                )
                if not allowed:
                    raise RateLimitError(retry_after=retry_after or 1)

    @app.after_request
    def _after(response):
        elapsed = None
        # 1) 响应头回写追踪信息
        response.headers["X-Request-ID"] = getattr(g, "trace_id", "-")
        if hasattr(g, "_request_start"):
            elapsed = time.perf_counter() - g._request_start
            response.headers["X-Query-Time"] = f"{elapsed:.3f}"
        # 2) 性能监控与慢查询告警（需求 3.3.4 用例“监控接口耗时”）
        from app.extensions import ext
        monitor = getattr(ext, "monitor", None)
        if monitor is not None and elapsed is not None and request.path.startswith("/api/"):
            monitor.record_request(request.method, request.path,
                                   response.status_code, elapsed)
            threshold = getattr(ext.settings, "slow_query_threshold_seconds", 5.0) or 0.0
            if threshold > 0 and elapsed >= threshold:
                logger.warning("慢查询告警: %s %s 耗时 %.3fs（阈值 %.1fs）",
                               request.method, request.path, elapsed, threshold)
        # 3) 兜底：视图直接 return dict/list 时自动包装为标准成功响应
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
        if int(e.code) >= 500:
            logger.error("业务异常告警 code=%s message=%s detail=%s",
                         int(e.code), e.message, e.detail)
        else:
            logger.warning("业务异常 code=%s message=%s detail=%s",
                           int(e.code), e.message, e.detail)
        _record_error(int(e.code))
        resp = jsonify(build(e.code, e.message, None if e.detail is None else {"detail": e.detail}))
        # 429 回写 Retry-After，调用方可据此退避重试
        if isinstance(e, RateLimitError):
            resp.headers["Retry-After"] = str(e.retry_after)
        return resp, int(e.code)

    @app.errorhandler(ValidationError)
    def _handle_validation(e: ValidationError):
        logger.warning("参数校验失败: %s", e.messages)
        _record_error(int(ErrorCode.PARAM_VALIDATION_ERROR))
        body = build(ErrorCode.PARAM_VALIDATION_ERROR, "参数校验失败", {"detail": e.messages})
        return jsonify(body), int(ErrorCode.PARAM_VALIDATION_ERROR)

    @app.errorhandler(HTTPException)
    def _handle_http(e: HTTPException):
        # 404/405/413 等 Flask 框架异常，映射到统一错误码
        code = e.code if e.code in (400, 404, 405, 408, 413, 415, 429) else 500
        logger.warning("HTTP 异常 %s %s", code, e.description)
        _record_error(code)
        body = build(ErrorCode(code), e.description or default_message(code))
        return jsonify(body), code

    @app.errorhandler(Exception)
    def _handle_unexpected(e: Exception):
        logger.exception("未预期异常: %s", e)
        if has_request_context():
            g.trace_id = getattr(g, "trace_id", "-")
        _record_error(int(ErrorCode.INTERNAL_ERROR))
        body = build(ErrorCode.INTERNAL_ERROR, default_message(ErrorCode.INTERNAL_ERROR))
        return jsonify(body), int(ErrorCode.INTERNAL_ERROR)
