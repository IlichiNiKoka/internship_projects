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
    CONFLICT = 409                # 资源版本冲突（CAS/乐观锁）
    REQUEST_ENTITY_TOO_LARGE = 413  # 请求体过大（二期修复：中间件映射需该枚举成员）
    PAYLOAD_TOO_LARGE = 413       # 旧名兼容别名（与 REQUEST_ENTITY_TOO_LARGE 同值同成员）
    UNSUPPORTED_MEDIA_TYPE = 415
    UNPROCESSABLE_ENTITY = 422
    TOO_MANY_REQUESTS = 429

    # ---- 5xx 服务端错误 ----
    INTERNAL_ERROR = 500          # 未预期异常
    COMPUTATION_ERROR = 500       # Spark/算法计算失败
    SERVICE_UNAVAILABLE = 503     # 数据未加载/依赖不可用
    GATEWAY_TIMEOUT = 504         # 计算超时


# 注意：业务子类错误码（PARAM_VALIDATION_ERROR / INVALID_FILTER 等）与 HTTP 码
# 同值，是同一枚举成员（alias）。若在此处为 alias 单独写消息，字典键会互相覆盖，
# 冲掉通用 HTTP 默认消息。本表每个 HTTP 码只保留一条通用默认消息；
# 业务子类的专属消息由各异常类自带 message 表达。
DEFAULT_MESSAGES: dict[int, str] = {
    int(ErrorCode.OK): "OK",
    int(ErrorCode.BAD_REQUEST): "请求格式错误",
    int(ErrorCode.UNAUTHORIZED): "未认证或凭证无效",
    int(ErrorCode.FORBIDDEN): "无权限访问该资源",
    int(ErrorCode.NOT_FOUND): "资源不存在",
    int(ErrorCode.METHOD_NOT_ALLOWED): "请求方法不被允许",
    ErrorCode.REQUEST_TIMEOUT: "请求处理超时",
    int(ErrorCode.CONFLICT): "资源版本冲突",
    int(ErrorCode.REQUEST_ENTITY_TOO_LARGE): "请求体过大（上限 2MB）",
    int(ErrorCode.UNSUPPORTED_MEDIA_TYPE): "Content-Type 必须为 application/json",
    int(ErrorCode.UNPROCESSABLE_ENTITY): "语义校验失败",
    int(ErrorCode.TOO_MANY_REQUESTS): "请求过于频繁，请稍后重试",
    int(ErrorCode.INTERNAL_ERROR): "服务内部错误",
    int(ErrorCode.SERVICE_UNAVAILABLE): "分析服务暂不可用（数据未就绪）",
    int(ErrorCode.GATEWAY_TIMEOUT): "聚合计算超时，请缩小查询范围后重试",
}


def default_message(code: ErrorCode | int) -> str:
    return DEFAULT_MESSAGES.get(int(code), "未知错误")
