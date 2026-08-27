# -*- coding: utf-8 -*-
"""3.3.1 多维度聚合分析 API。

POST /api/v1/aggregations/run
  请求体示例：
  {
    "dimensions": ["age_group", "gender"],
    "metrics": ["discharge_count", "avg_length_of_stay", "avg_total_charges"],
    "filters": [
      {"field": "discharge_year", "op": "eq", "value": 2021},
      {"field": "total_charges", "op": "gte", "value": 10000}
    ],
    "sort": [{"field": "discharge_count", "order": "desc"}],
    "limit": 50
  }
"""

from __future__ import annotations

from flask import Blueprint, request

from app.core.response import success
from app.extensions import ext
from app.schemas.aggregation import AggregationBatchRequestSchema, AggregationRequestSchema
from app.services.aggregation_service import AggregationService

bp = Blueprint("aggregation", __name__, url_prefix="/api/v1/aggregations")


@bp.post("/run")
def run_aggregation():
    """执行多维度聚合分析（维度/指标/过滤/排序/条数）。"""
    payload = AggregationRequestSchema().load(request.get_json(silent=True) or {})
    service = AggregationService(
        provider=ext.data_provider,
        cache=ext.cache,
        settings=ext.settings,
    )
    result = service.run(payload)
    return success(result)


@bp.post("/batch")
def run_aggregation_batch():
    """批量聚合：一次请求内共享同一组过滤条件执行多个分组聚合。

    大屏筛选联动优化：过滤只应用一次（Spark 缓存过滤结果供子查询复用），
    单个 HTTP 请求替代 N 个并发请求，整批结果一份缓存。

    请求体示例：
      {
        "filters": [{"field": "discharge_year", "op": "eq", "value": 2021}],
        "queries": [
          {"id": "kpi", "dimensions": ["discharge_year"], "metrics": ["discharge_count"]},
          {"id": "age", "dimensions": ["age_group"], "metrics": ["discharge_count"],
           "sort": [{"field": "discharge_count", "order": "desc"}], "limit": 10}
        ]
      }

    响应 data 结构：{ results: { <id>: AggregationResponse }, query_count, compute_seconds, cached }
    """
    payload = AggregationBatchRequestSchema().load(request.get_json(silent=True) or {})
    service = AggregationService(
        provider=ext.data_provider,
        cache=ext.cache,
        settings=ext.settings,
    )
    result = service.run_batch(payload.get("filters") or [], payload["queries"])
    return success(result)
