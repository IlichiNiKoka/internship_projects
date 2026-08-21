# -*- coding: utf-8 -*-
"""人员3分析能力的调用客户端（本地依赖注入 / REST 两种实现）。"""

from __future__ import annotations

import json
import socket
from abc import ABC, abstractmethod
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest


class AnalysisAPIError(RuntimeError):
    """下游分析 API 调用错误；``retryable`` 供工具执行器决定是否重试。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        detail: Any = None,
        trace_id: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.detail = detail
        self.trace_id = trace_id


class AnalysisClient(ABC):
    """人员1只依赖此契约，不依赖 Spark 或 Flask 视图实现。"""

    mode = "unknown"

    @abstractmethod
    def run_aggregation(self, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def run_algorithm(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def metadata(self, kind: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


class LocalAnalysisClient(AnalysisClient):
    """单体部署使用的进程内客户端，调用与 REST 相同的服务层契约。"""

    mode = "local"

    def __init__(self, aggregation_service, algorithm_service):
        self._aggregation = aggregation_service
        self._algorithm = algorithm_service

    def run_aggregation(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._aggregation.run(params)

    def run_algorithm(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._algorithm.run(name, params)

    def metadata(self, kind: str | None = None) -> dict[str, Any]:
        # 延迟导入，保证 FakeClient 的纯单元测试不需要 PySpark。
        from app.algorithms.base import list_algorithms
        from config.registry import dimension_meta, metric_meta

        if kind == "dimensions":
            return {"dimensions": dimension_meta()}
        if kind == "metrics":
            return {"metrics": metric_meta()}
        if kind == "algorithms":
            return {"algorithms": list_algorithms()}
        return {
            "dimensions": dimension_meta(),
            "metrics": metric_meta(),
            "algorithms": list_algorithms(),
        }


class HTTPAnalysisClient(AnalysisClient):
    """通过人员3 REST API 调用分析服务，适用于拆分部署。"""

    mode = "http"

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        api_key: str = "",
        opener=None,
    ):
        if not base_url or not base_url.strip():
            raise ValueError("HTTP 分析客户端必须配置 analysis_api_base_url")
        self._base_url = base_url.rstrip("/")
        self._timeout = max(0.1, float(timeout))
        self._api_key = api_key
        self._opener = opener or urlrequest.urlopen

    def run_aggregation(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/aggregations/run", params)

    def run_algorithm(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        safe_names = {
            "statistics", "association", "cost_prediction",
            "readmission_risk", "group_aggregation",
        }
        if name not in safe_names:
            raise AnalysisAPIError(
                f"不支持的算法工具: {name}", status_code=400, retryable=False
            )
        return self._request(
            "POST", f"/api/v1/algorithms/{name}/run", {"params": params}
        )

    def metadata(self, kind: str | None = None) -> dict[str, Any]:
        allowed = {None, "dimensions", "metrics", "algorithms"}
        if kind not in allowed:
            raise AnalysisAPIError(
                f"不支持的元数据类型: {kind}", status_code=400, retryable=False
            )
        if kind:
            return {kind: self._request("GET", f"/api/v1/meta/{kind}")}
        return {
            item: self._request("GET", f"/api/v1/meta/{item}")
            for item in ("dimensions", "metrics", "algorithms")
        }

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        req = urlrequest.Request(
            f"{self._base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            response = self._opener(req, timeout=self._timeout)
            try:
                raw = response.read()
                status = int(getattr(response, "status", 200))
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        # 已完整读取响应时，连接回收失败不应覆盖业务结果。
                        pass
        except urlerror.HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
            try:
                parsed = self._parse_json(raw)
            except AnalysisAPIError:
                # 错误页常由网关以 HTML/纯文本返回。正文格式不能覆盖真实
                # HTTP 状态，否则 408/429/5xx 会被误判为不可重试。
                parsed = None
            raise self._build_error(status, parsed) from exc
        except (urlerror.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise AnalysisAPIError(
                "分析服务连接失败",
                retryable=True,
                detail={"error_type": type(exc).__name__},
            ) from exc

        parsed = self._parse_json(raw)
        if not 200 <= status < 300:
            raise self._build_error(status, parsed)
        if not isinstance(parsed, dict):
            raise AnalysisAPIError("分析服务返回了非 JSON 对象", retryable=False)

        # 人员3统一响应外壳：HTTP 2xx 仍校验 body.code，防代理改写状态。
        if {"code", "message", "data"}.issubset(parsed):
            code = int(parsed.get("code") or status)
            if not 200 <= code < 300:
                raise self._build_error(code, parsed)
            return parsed.get("data")
        return parsed

    @staticmethod
    def _parse_json(raw: bytes | str) -> Any:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnalysisAPIError("分析服务响应不是有效 JSON", retryable=False) from exc

    @staticmethod
    def _build_error(status: int, body: Any) -> AnalysisAPIError:
        retryable = status in {408, 429, 502, 503, 504}
        message = "分析服务调用失败"
        detail = None
        trace_id = None
        if isinstance(body, dict):
            message = str(body.get("message") or message)
            detail = body.get("data")
            trace_id = body.get("trace_id")
        return AnalysisAPIError(
            message,
            status_code=status,
            retryable=retryable,
            detail=detail,
            trace_id=trace_id,
        )
