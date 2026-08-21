# -*- coding: utf-8 -*-
"""业务异常体系。

所有可预期错误统一抛出 ``BizException`` 子类，由中间件转换为
标准 JSON 响应。
"""

from __future__ import annotations

from app.core.error_codes import ErrorCode, default_message


class BizException(Exception):
    """业务异常基类：携带统一错误码、消息与可选明细。"""

    def __init__(
        self,
        code: ErrorCode | int = ErrorCode.BAD_REQUEST,
        message: str | None = None,
        detail: object = None,
    ):
        self.code = ErrorCode(code)
        self.message = message or default_message(self.code)
        self.detail = detail
        super().__init__(self.message)


# ---- 4xx：参数/资源类 ----
class ParamValidationError(BizException):
    """请求参数结构校验失败。"""

    def __init__(self, detail: object = None, message: str | None = None):
        super().__init__(
            ErrorCode.PARAM_VALIDATION_ERROR,
            message or "参数校验失败",
            detail,
        )


class InvalidDimensionError(ParamValidationError):
    def __init__(self, invalid: object, available: list[str] | None = None):
        super().__init__(
            detail={"invalid_dimensions": invalid, "available": available},
            message=f"包含不支持的聚合维度: {invalid}",
        )


class InvalidMetricError(ParamValidationError):
    def __init__(self, invalid: object, available: list[str] | None = None):
        super().__init__(
            detail={"invalid_metrics": invalid, "available": available},
            message=f"包含不支持的聚合指标: {invalid}",
        )


class InvalidFilterError(ParamValidationError):
    def __init__(self, message: str = "过滤条件不合法", detail: object = None):
        super().__init__(detail=detail, message=message)


class ResourceNotFoundError(BizException):
    def __init__(self, message: str | None = None):
        super().__init__(ErrorCode.NOT_FOUND, message)


class ConflictError(BizException):
    """资源已被并发更新，当前快照不能继续提交。"""

    def __init__(self, message: str | None = None, detail: object = None):
        super().__init__(ErrorCode.CONFLICT, message, detail)


class TooManyRequestsError(BizException):
    """资源正在被另一个请求处理，调用方应稍后重试。"""

    def __init__(self, message: str | None = None, detail: object = None):
        super().__init__(ErrorCode.TOO_MANY_REQUESTS, message, detail)


class AlgorithmNotFoundError(BizException):
    def __init__(self, name: str):
        super().__init__(
            ErrorCode.ALGORITHM_NOT_FOUND,
            message=f"算法组件未注册: {name}",
            detail={"algorithm": name},
        )


# ---- 5xx：服务端类 ----
class ComputationError(BizException):
    """Spark 计算任务失败。"""

    def __init__(self, message: str | None = None, detail: object = None):
        super().__init__(
            ErrorCode.COMPUTATION_ERROR,
            message or "大数据计算任务执行失败",
            detail,
        )


class ServiceUnavailableError(BizException):
    def __init__(self, message: str | None = None, detail: object = None):
        super().__init__(ErrorCode.SERVICE_UNAVAILABLE, message, detail)


class ComputationTimeoutError(BizException):
    def __init__(self, seconds: float, detail: object = None):
        super().__init__(
            ErrorCode.GATEWAY_TIMEOUT,
            message=f"聚合计算超时（阈值 {seconds:.1f}s），请缩小查询范围后重试",
            detail=detail,
        )
