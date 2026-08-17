# -*- coding: utf-8 -*-
"""元数据 API：维度/指标/算法清单，供 AI 智能交互模块动态发现可用分析能力。"""

from __future__ import annotations

from flask import Blueprint

from app.algorithms.base import list_algorithms
from app.core.response import success
from app.extensions import ext
from config.registry import dimension_meta, metric_meta

bp = Blueprint("meta", __name__, url_prefix="/api/v1/meta")


@bp.get("/dimensions")
def dimensions():
    """可用聚合维度清单。"""
    return success(dimension_meta())


@bp.get("/metrics")
def metrics():
    """可用聚合指标清单。"""
    return success(metric_meta())


@bp.get("/algorithms")
def algorithms():
    """已注册算法组件清单。"""
    return success(list_algorithms())


@bp.get("/cache")
def cache_stats():
    """缓存运行状态（命中率等）。"""
    return success(ext.cache.stats if ext.cache else {})
