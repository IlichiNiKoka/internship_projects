# -*- coding: utf-8 -*-
"""统一错误码体系（需求 3.3.3）。

HTTP 状态码与响应体 ``code`` 保持一致。业务子类通过明确的
``message`` 和可选 ``detail`` 表达，相同 HTTP 状态不再在默认文案
字典中互相覆盖。
"""

from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    # ---- 2xx 成功 ----
    OK = 200

    # ---- 4xx 客户端错误 ----
    BAD_REQUEST = 400
    PARAM_VALIDATION_ERROR = 400
    INVALID_DIMENSION = 400
    INVALID_METRIC = 400
    INVALID_FILTER = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    ALGORITHM_NOT_FOUND = 404
    METHOD_NOT_ALLOWED = 405
    REQUEST_TIMEOUT = 408
    CONFLICT = 409
    PAYLOAD_TOO_LARGE = 413
    UNSUPPORTED_MEDIA_TYPE = 415
    UNPROCESSABLE_ENTITY = 422
    TOO_MANY_REQUESTS = 429

    # ---- 5xx 服务端错误 ----
    INTERNAL_ERROR = 500
    COMPUTATION_ERROR = 500
    SERVICE_UNAVAILABLE = 503
    GATEWAY_TIMEOUT = 504


# 同一数值的 IntEnum 成员是别名，因此这里每个 HTTP 码只保留一条
# 通用默认文案；业务异常子类负责提供更精确的消息。
DEFAULT_MESSAGES: dict[int, str] = {
    ErrorCode.OK: "OK",
    ErrorCode.BAD_REQUEST: "请求格式错误",
    ErrorCode.UNAUTHORIZED: "未认证或凭证无效",
    ErrorCode.FORBIDDEN: "无权限访问该资源",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不被允许",
    ErrorCode.REQUEST_TIMEOUT: "请求处理超时",
    ErrorCode.CONFLICT: "资源状态冲突，请刷新后重试",
    ErrorCode.PAYLOAD_TOO_LARGE: "请求体过大",
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: "Content-Type 必须为 application/json",
    ErrorCode.UNPROCESSABLE_ENTITY: "语义校验失败",
    ErrorCode.TOO_MANY_REQUESTS: "请求过于频繁，请稍后重试",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
    ErrorCode.SERVICE_UNAVAILABLE: "分析服务暂不可用（数据未就绪）",
    ErrorCode.GATEWAY_TIMEOUT: "聚合计算超时，请缩小查询范围后重试",
}


def default_message(code: ErrorCode | int) -> str:
    return DEFAULT_MESSAGES.get(int(code), "未知错误")
