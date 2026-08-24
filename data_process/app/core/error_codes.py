# -*- coding: utf-8 -*-
"""统一错误码体系（需求 3.3.3）。

约定：
  * 2xx —— 成功（200 成功）
  * 4xx —— 客户端错误（参数、权限、资源不存在、请求频率等）
  * 5xx —— 服务端错误（内部异常、依赖不可用、计算超时等）
  * HTTP 状态码与响应体 code 保持一致，调用方只需看 code 即可分支处理。

业务子类错误通过 `message` 与可选 `detail` 表达，不引入新 code 位，
避免错误码膨胀、便于 AI 交互模块统一解析。
"""

from __future__ import annotations

from enum import IntEnum


class ErrorCode(IntEnum):
    # ---- 2xx 成功 ----
    OK = 200

    # ---- 4xx 客户端错误 ----
    BAD_REQUEST = 400              # 请求体缺失/JSON 格式错误
    PARAM_VALIDATION_ERROR = 400  # 参数结构校验失败（marshmallow）
    INVALID_DIMENSION = 400       # 维度不在注册表
    INVALID_METRIC = 400          # 指标不在注册表
    INVALID_FILTER = 400          # 过滤条件非法
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404               # 路由不存在
    ALGORITHM_NOT_FOUND = 404     # 算法未注册
    METHOD_NOT_ALLOWED = 405
    REQUEST_TIMEOUT = 408
    CONFLICT = 409                 # 并发冲突（会话/快照版本冲突等）
    REQUEST_ENTITY_TOO_LARGE = 413  # 请求体过大（二期修复：中间件映射需该枚举成员）
    UNSUPPORTED_MEDIA_TYPE = 415
    UNPROCESSABLE_ENTITY = 422
    TOO_MANY_REQUESTS = 429

    # ---- 5xx 服务端错误 ----
    INTERNAL_ERROR = 500          # 未预期异常
    COMPUTATION_ERROR = 500       # Spark/算法计算失败
    SERVICE_UNAVAILABLE = 503     # 数据未加载/依赖不可用
    GATEWAY_TIMEOUT = 504         # 计算超时


DEFAULT_MESSAGES: dict[int, str] = {
    ErrorCode.OK: "OK",
    ErrorCode.BAD_REQUEST: "请求格式错误",
    ErrorCode.PARAM_VALIDATION_ERROR: "参数校验失败",
    ErrorCode.INVALID_DIMENSION: "包含不支持的聚合维度",
    ErrorCode.INVALID_METRIC: "包含不支持的聚合指标",
    ErrorCode.INVALID_FILTER: "过滤条件不合法",
    ErrorCode.UNAUTHORIZED: "未认证或凭证无效",
    ErrorCode.FORBIDDEN: "无权限访问该资源",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.ALGORITHM_NOT_FOUND: "算法组件未注册",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不被允许",
    ErrorCode.CONFLICT: "资源状态冲突，请刷新后重试",
    ErrorCode.REQUEST_TIMEOUT: "请求处理超时",
    ErrorCode.REQUEST_ENTITY_TOO_LARGE: "请求体过大（上限 2MB）",
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: "Content-Type 必须为 application/json",
    ErrorCode.UNPROCESSABLE_ENTITY: "语义校验失败",
    ErrorCode.TOO_MANY_REQUESTS: "请求过于频繁，请稍后重试",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
    ErrorCode.COMPUTATION_ERROR: "大数据计算任务执行失败",
    ErrorCode.SERVICE_UNAVAILABLE: "分析服务暂不可用（数据未就绪）",
    ErrorCode.GATEWAY_TIMEOUT: "聚合计算超时，请缩小查询范围后重试",
}


def default_message(code: ErrorCode | int) -> str:
    return DEFAULT_MESSAGES.get(int(code), "未知错误")
