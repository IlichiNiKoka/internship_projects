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
from app.schemas.aggregation import AggregationRequestSchema
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
