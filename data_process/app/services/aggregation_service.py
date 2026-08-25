# -*- coding: utf-8 -*-
"""多维度聚合分析服务（需求 3.3.1）。

职责：
  * 参数白名单校验（维度/指标/过滤/排序，防注入）；
  * 组装 Spark 分组聚合任务（GROUP BY + COUNT/AVG/SUM/MAX）；
  * 结果归一化（数值精度统一、NaN→null、字段命名统一）；
  * 结果缓存（命中缓存直接返回，见需求流程图“缓存查询结果”扩展用例）。

本服务不依赖 Flask：既供 REST API 调用，也供算法组件 group_aggregation 复用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from functools import reduce
from typing import Any

from pyspark.sql import Column, DataFrame, functions as F

from app.core.cache import NullCache
from app.core.exceptions import (
    ComputationError,
    ComputationTimeoutError,
    InvalidDimensionError,
    InvalidFilterError,
    InvalidMetricError,
)
from app.core.timeout import run_with_timeout
from config.registry import (
    DIMENSIONS,
    METRICS,
    NUMERIC_FILTER_COLUMNS,
    SORTABLE_FIELDS,
    STRING_FILTER_OPS,
    DimensionSpec,
    MetricSpec,
    resolve_dimension,
    resolve_metric,
)
from config.settings import Settings

logger = logging.getLogger(__name__)


class _SingleFlight:
    """同 key 并发去重：相同 cache_key 的并发请求只放第一个去算，其余等待后读缓存。

    前端大屏首次打开会同时发出多个完全相同的聚合请求，若无去重，
    每个请求都会触发一次 Spark 作业白白抢占资源；
    single-flight 保证同一 key 只有一个 Spark 作业在跑。
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            lock.acquire()
            return lock


_SINGLE_FLIGHT = _SingleFlight()


def _safe_value(value: Any) -> Any:
    """NaN/Inf 不能进入 JSON，统一转为 null。"""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


class AggregationService:
    """多维度聚合分析核心服务。"""

    def __init__(
        self,
        provider=None,           # DataProvider（惰性取数）；与 df 二选一
        df: DataFrame | None = None,
        cache=None,              # CacheBackend，默认 NullCache
        settings: Settings | None = None,
        timeout_seconds: float | None = None,   # 二期：计算超时阈值，显式传入优先于配置
    ):
        self._provider = provider
        self._df = df
        self._cache = cache or NullCache()
        self._settings = settings
        self._max_limit = settings.agg_max_limit if settings else 1000
        self._default_limit = settings.agg_default_limit if settings else 100
        self._max_dimensions = settings.agg_max_dimensions if settings else 5
        # 二期 3.3.4：超时阈值（<=0 不限制）与慢查询阈值（<=0 不告警）
        if timeout_seconds is not None:
            self._timeout_seconds = timeout_seconds
        else:
            self._timeout_seconds = settings.agg_timeout_seconds if settings else None
        self._slow_threshold = (settings.slow_query_threshold_seconds if settings else 5.0) or 0.0

    # ------------------------------------------------------------------
    def run(self, request: dict, use_cache: bool = True) -> dict:
        """执行一次聚合分析，返回归一化结果字典。

        request 结构（见 schemas/aggregation.py）：
          dimensions: list[str]  维度 key（必填，1~5 个）
          metrics:    list[str]  指标 key（必填，至少 1 个）
          filters:    list[dict] 过滤条件（可选）
          sort:       list[dict] 排序规则（可选，默认按首个指标降序）
          limit:      int        返回行数（可选，默认由配置决定）
          page/page_size: int    大结果集分页（二期 3.3.4，任一出现即启用分页）
        """
        # ---- 1. 白名单校验与解析（含二期分页参数）----
        dimensions, metrics, limit, page, page_size = self._validate_request(request)
        filters = self._normalize_filters(request.get("filters") or [])
        sort_specs = self._normalize_sort(request.get("sort") or [], metrics)
        use_pagination = page is not None

        # ---- 2. 缓存查询 + 同 key 并发去重（single-flight）----
        cache_key = self._cache_key(dimensions, metrics, filters, sort_specs,
                                    limit, page, page_size)
        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.info("聚合结果命中缓存: %s", cache_key)
                cached["cached"] = True
                return cached

        # 同 key 并发去重：第一个请求持锁计算，其余等锁释放后直接读缓存；
        # 若等待后缓存仍未命中（前一个请求失败），则自己重算一次。
        flight_lock = _SINGLE_FLIGHT.acquire(cache_key)
        try:
            if use_cache:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    logger.info("聚合结果命中缓存(去重等待后): %s", cache_key)
                    cached["cached"] = True
                    return cached
            return self._compute_and_cache(
                cache_key, dimensions, metrics, filters, sort_specs,
                limit, page, page_size, use_pagination, use_cache)
        finally:
            flight_lock.release()

    def _compute_and_cache(self, cache_key, dimensions, metrics, filters,
                           sort_specs, limit, page, page_size,
                           use_pagination: bool, use_cache: bool) -> dict:
        """实际执行 Spark 聚合并写缓存（由 single-flight 互斥调用）。"""
        # ---- 3. Spark 分组聚合计算（二期 3.3.4：超时控制 -> 504 降级）----
        fetch_limit = page * page_size if use_pagination else limit
        start = time.perf_counter()
        try:
            df = self._dataframe()
            filtered = self._apply_filters(df, filters)
            grouped = filtered.groupBy(*[d.column for d in dimensions])
            agg_result = grouped.agg(*[m.agg_expr() for m in metrics])

            # 排序：默认按首个指标降序
            order_cols = [
                F.col(spec["field"]).desc() if spec["order"] == "desc" else F.col(spec["field"]).asc()
                for spec in sort_specs
            ]
            ordered = agg_result.orderBy(*order_cols)

            raw_rows = run_with_timeout(
                lambda: [r.asDict(recursive=True) for r in ordered.limit(fetch_limit).collect()],
                timeout_seconds=self._timeout_seconds,
                task_name=f"聚合分析({len(dimensions)}维x{len(metrics)}指标)",
            )
        except ComputationTimeoutError:
            raise   # 超时降级（504）由中间件统一响应，不包成 500
        except Exception as exc:
            logger.exception("聚合计算失败")
            raise ComputationError(
                message="Spark 聚合计算失败，请检查参数后重试",
                detail={"error": str(exc)},
            ) from exc

        elapsed = round(time.perf_counter() - start, 3)

        # ---- 3.5 慢查询日志与告警（二期 3.3.4 备选流B）----
        if self._slow_threshold > 0 and elapsed >= self._slow_threshold:
            logger.warning(
                "慢查询告警: 聚合查询耗时 %.3fs（阈值 %.1fs）cache_key=%s",
                elapsed, self._slow_threshold, cache_key,
            )

        # ---- 4. 分页裁剪 + 结果归一化（数值精度、NaN、字段命名）----
        if use_pagination:
            begin = (page - 1) * page_size
            rows = raw_rows[begin:begin + page_size]
            has_more = len(raw_rows) == fetch_limit   # 取满 fetch_limit 说明可能还有下一页
        else:
            rows = raw_rows
            has_more = None

        normalized_rows = self._normalize_rows(rows, metrics)

        result = {
            "dimensions": [{"key": d.key, "column": d.column, "label": d.label_cn}
                           for d in dimensions],
            "metrics": [{"key": m.key, "label": m.label_cn, "unit": m.unit}
                        for m in metrics],
            "filters": filters,
            "sort": sort_specs,
            "rows": normalized_rows,
            "row_count": len(normalized_rows),
            "truncated": has_more if use_pagination else len(raw_rows) >= limit,
            "cached": False,
            "compute_seconds": elapsed,
        }
        if use_pagination:
            result["pagination"] = {
                "page": page,
                "page_size": page_size,
                "returned": len(normalized_rows),
                "has_more": has_more,
            }

        # ---- 5. 写缓存 ----
        if use_cache:
            self._cache.set(cache_key, dict(result))
        return result

    # ------------------------------------------------------------------
    def _dataframe(self) -> DataFrame:
        if self._df is not None:
            return self._df
        if self._provider is not None:
            return self._provider.dataframe()
        raise ComputationError("数据源未配置（provider 与 df 均为空）")

    # ---- 校验 ----
    def _validate_request(self, request: dict) -> tuple[list[DimensionSpec], list[MetricSpec], int, int | None, int | None]:
        dim_keys = request.get("dimensions") or []
        metric_keys = request.get("metrics") or []

        if not dim_keys:
            raise InvalidDimensionError(invalid=[], available=None)
        if len(dim_keys) > self._max_dimensions:
            raise InvalidDimensionError(
                invalid=dim_keys,
                available=None,
            )
        if not metric_keys:
            raise InvalidMetricError(invalid=[])

        dimensions, invalid = [], []
        for key in dim_keys:
            spec = resolve_dimension(key)
            if spec is None:
                invalid.append(key)
            elif spec not in dimensions:
                dimensions.append(spec)
        if invalid:
            raise InvalidDimensionError(invalid, available=[d.key for d in DIMENSIONS])

        metrics, invalid_m = [], []
        for key in metric_keys:
            spec = resolve_metric(key)
            if spec is None:
                invalid_m.append(key)
            elif spec not in metrics:
                metrics.append(spec)
        if invalid_m:
            raise InvalidMetricError(invalid_m, available=[m.key for m in METRICS])

        # ---- 二期 3.3.4：大结果集分页（page/page_size 任一出现即启用分页模式）----
        raw_page = request.get("page")
        raw_page_size = request.get("page_size")
        if raw_page is not None or raw_page_size is not None:
            page = max(1, int(raw_page or 1))
            page_size = raw_page_size or request.get("limit") or self._default_limit
            page_size = max(1, min(int(page_size), self._max_limit))
            limit = page_size
        else:
            page, page_size = None, None
            limit = request.get("limit") or self._default_limit
            limit = max(1, min(int(limit), self._max_limit))
        return dimensions, metrics, limit, page, page_size

    # ---- 过滤条件规范化与执行 ----
    def _normalize_filters(self, filters: list[dict]) -> list[dict]:
        normalized = []
        for item in filters:
            field = item.get("field")
            op = item.get("op")
            if not field or not op:
                raise InvalidFilterError(detail=item, message="过滤条件缺少 field/op")

            dim = resolve_dimension(field)
            if dim is not None:
                col_name, value_type = dim.column, dim.value_type
            elif field in NUMERIC_FILTER_COLUMNS:
                col_name, value_type = field, "number"
            else:
                raise InvalidFilterError(
                    detail={"field": field},
                    message=f"过滤字段不在白名单: {field}",
                )

            if value_type == "number":
                allowed_ops = {"eq", "ne", "in", "not_in", "gte", "gt", "lte", "lt", "between"}
            else:
                allowed_ops = STRING_FILTER_OPS
            if op not in allowed_ops:
                raise InvalidFilterError(
                    detail={"field": field, "op": op},
                    message=f"字段 {field}({value_type}) 不支持操作符 {op}，可用: {sorted(allowed_ops)}",
                )

            # 单值 / 多值结构校验
            if op in ("in", "not_in", "between"):
                values = item.get("values")
                if not isinstance(values, list) or not values:
                    raise InvalidFilterError(detail=item, message=f"操作符 {op} 需要非空 values 列表")
                if op == "between" and len(values) != 2:
                    raise InvalidFilterError(detail=item, message="between 需要恰好 2 个值")
            else:
                value = item.get("value")
                if value is None:
                    raise InvalidFilterError(detail=item, message=f"操作符 {op} 需要 value")

            normalized.append({
                "field": col_name,
                "requested_field": field,
                "op": op,
                "value": item.get("value"),
                "values": item.get("values"),
            })
        return normalized

    @staticmethod
    def _apply_filters(df: DataFrame, filters: list[dict]) -> DataFrame:
        if not filters:
            return df
        exprs: list[Column] = []
        for f in filters:
            col = F.col(f["field"])
            op = f["op"]
            if op == "eq":
                exprs.append(col == f["value"])
            elif op == "ne":
                exprs.append(col != f["value"])
            elif op == "in":
                exprs.append(col.isin(f["values"]))
            elif op == "not_in":
                exprs.append(~col.isin(f["values"]))
            elif op == "gte":
                exprs.append(col >= float(f["value"]))
            elif op == "gt":
                exprs.append(col > float(f["value"]))
            elif op == "lte":
                exprs.append(col <= float(f["value"]))
            elif op == "lt":
                exprs.append(col < float(f["value"]))
            elif op == "between":
                lo, hi = float(f["values"][0]), float(f["values"][1])
                exprs.append(col.between(lo, hi))
            else:  # pragma: no cover
                raise InvalidFilterError(detail=f, message=f"不支持的操作符: {op}")
        return df.filter(reduce(lambda a, b: a & b, exprs))

    # ---- 排序规范化 ----
    @staticmethod
    def _normalize_sort(sort: list[dict], metrics: list[MetricSpec]) -> list[dict]:
        metric_keys = {m.key for m in metrics}
        dimension_cols = {d.column for d in DIMENSIONS}
        specs = []
        for item in sort:
            field = item.get("field")
            order = item.get("order", "desc")
            if order not in ("asc", "desc"):
                raise InvalidFilterError(detail=item, message=f"排序方向非法: {order}")
            if field not in SORTABLE_FIELDS:
                raise InvalidFilterError(detail=item, message=f"排序字段不在白名单: {field}")
            specs.append({"field": field, "order": order})
        if not specs:
            specs = [{"field": metrics[0].key, "order": "desc"}]
        return specs

    # ---- 归一化与缓存键 ----
    @staticmethod
    def _normalize_rows(rows: list[dict], metrics: list[MetricSpec]) -> list[dict]:
        decimals = {m.key: m.decimals for m in metrics}
        normalized = []
        for row in rows:
            out = {}
            for key, value in row.items():
                value = _safe_value(value)
                if key in decimals and isinstance(value, float):
                    value = round(value, decimals[key])
                out[key] = value
            normalized.append(out)
        return normalized

    @staticmethod
    def _cache_key(dimensions, metrics, filters, sort_specs, limit,
                   page=None, page_size=None) -> str:
        payload = json.dumps({
            "d": [d.column for d in dimensions],
            "m": [m.key for m in metrics],
            "f": filters,
            "s": sort_specs,
            "l": limit,
            "p": page,       # 二期：分页参数参与缓存键，避免页间串数据
            "ps": page_size,
        }, sort_keys=True, ensure_ascii=True)
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:32]
        return f"agg:{digest}"
