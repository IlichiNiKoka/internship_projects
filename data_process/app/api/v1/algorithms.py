# -*- coding: utf-8 -*-
"""3.3.2 大数据算法统一调用 API。

POST /api/v1/algorithms/<name>/run  统一算法执行接口
GET  /api/v1/algorithms/<name>      算法元信息（参数规格、说明）
"""

from __future__ import annotations

from flask import Blueprint, request

from app.algorithms.base import get_algorithm
from app.core.response import success
from app.extensions import ext
from app.schemas.algorithm import AlgorithmRunSchema
from app.services.algorithm_service import AlgorithmService

bp = Blueprint("algorithms", __name__, url_prefix="/api/v1/algorithms")


@bp.post("/<name>/run")
def run_algorithm(name: str):
    """统一算法调用接口：选择算法组件并传入参数，返回归一化结果。"""
    payload = AlgorithmRunSchema().load(request.get_json(silent=True) or {})
    service = AlgorithmService(ext.data_provider)
    result = service.run(name, payload.get("params") or {})
    return success(result)


@bp.get("/<name>")
def algorithm_meta(name: str):
    """查询算法组件元信息。"""
    return success(get_algorithm(name).meta())
