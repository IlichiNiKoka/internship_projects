# -*- coding: utf-8 -*-
"""统一 JSON 响应封装（需求 3.3.3）。

统一结构（所有接口一致）：
    {
      "code": 200,                 # 与 HTTP 状态码一致，见 error_codes.py
      "message": "OK",
      "data": {...},               # 业务数据（成功时存在）
      "query_time": 0.123,         # 接口处理耗时（秒，保留 3 位）
      "trace_id": "xxxxxxxx"       # 请求追踪 ID（透传 X-Request-ID 或自动生成）
    }
"""

from __future__ import annotations

import time
import uuid

from flask import Response, g, has_app_context, has_request_context
from flask.json import dumps

from app.core.error_codes import ErrorCode, default_message


def _current_trace_id() -> str:
    if has_request_context():
        return getattr(g, "trace_id", None) or _new_trace_id()
    return _new_trace_id()


def _new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def _elapsed_seconds() -> float:
    """自请求开始（中间件记录）以来的耗时；无请求上下文时为 0。"""
    if has_request_context():
        start = getattr(g, "_request_start", None)
        if start is not None:
            return round(time.perf_counter() - start, 3)
    return 0.0


def build(code: ErrorCode | int, message: str, data=None,
          query_time: float | None = None, trace_id: str | None = None) -> dict:
    """组装标准响应体（纯数据，不含 Flask Response 包装）。"""
    return {
        "code": int(code),
        "message": message,
        "data": data,
        "query_time": query_time if query_time is not None else _elapsed_seconds(),
        "trace_id": trace_id or _current_trace_id(),
    }


def _json_response(body: dict, status: int):
    """构造 JSON 响应：有应用上下文用 jsonify（响应头处理一致），
    无上下文（单元测试直调 helper）时直接构造 Response，保证可测试性。"""
    if has_app_context():
        from flask import jsonify
        return jsonify(body), status
    resp = Response(dumps(body, ensure_ascii=False), status=status,
                    mimetype="application/json")
    return resp, status


def success(data=None, message: str = "OK",
            query_time: float | None = None, trace_id: str | None = None):
    """成功响应助手：视图函数 return success(data) 即可。"""
    body = build(ErrorCode.OK, message, data, query_time, trace_id)
    return _json_response(body, int(ErrorCode.OK))


def error(code: ErrorCode | int, message: str | None = None, detail=None,
          query_time: float | None = None, trace_id: str | None = None):
    """错误响应助手：code 同时决定 HTTP 状态码。"""
    body = build(ErrorCode(code), message or default_message(code),
                 None if detail is None else {"detail": detail},
                 query_time, trace_id)
    return _json_response(body, int(code))
