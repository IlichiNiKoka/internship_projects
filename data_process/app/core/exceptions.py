# -*- coding: utf-8 -*-
"""业务异常体系：所有可预期错误统一抛 BizException 子类，
由中间件按错误码表转换为标准 JSON 响应（见 core/middleware.py）。
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
    """请求参数结构校验失败（marshmallow 校验消息走 detail）。"""

    def __init__(self, detail: object = None, message: str | None = None):
        super().__init__(ErrorCode.PARAM_VALIDATION_ERROR, message, detail)


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
    """资源状态冲突（409），如幂等键重复使用或会话版本冲突。"""

    def __init__(self, message: str | None = None, detail: object = None):
        super().__init__(ErrorCode.CONFLICT, message, detail)


# ---- 4xx：认证 / 权限 / 限流类（二期 3.3.5）----
class UnauthorizedError(BizException):
    """未携带认证凭证（401）。"""

    def __init__(self, detail: object = None, message: str | None = None):
        super().__init__(ErrorCode.UNAUTHORIZED, message, detail)


class ForbiddenError(BizException):
    """认证凭证无效或无权限访问（403）。"""

    def __init__(self, detail: object = None, message: str | None = None):
        super().__init__(ErrorCode.FORBIDDEN, message, detail)


class TooManyRequestsError(BizException):
    """请求频率超限或会话锁竞争失败（429）。"""

    def __init__(self, message: str | None = None, detail: object = None):
        super().__init__(ErrorCode.TOO_MANY_REQUESTS, message, detail)


class RateLimitError(BizException):
    """请求频率超限（429），detail 携带重试等待秒数。"""

    def __init__(self, retry_after: float | int = 1, detail: object = None,
                 message: str | None = None):
        self.retry_after = max(1, int(retry_after))
        if detail is None:
            detail = {"retry_after_seconds": self.retry_after}
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
        super().__init__(ErrorCode.COMPUTATION_ERROR, message, detail)


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
