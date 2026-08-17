# -*- coding: utf-8 -*-
"""算法组件 1：分组聚合（group_aggregation）。

把 3.3.1 多维度聚合分析的计算逻辑以统一算法接口封装，
聚合分析 API 与 AI 智能交互模块共用同一实现，保证口径一致。
"""

from __future__ import annotations

from typing import Any

from app.algorithms.base import Algorithm, AlgorithmContext, ParamSpec, register_algorithm
from app.services.aggregation_service import AggregationService


@register_algorithm
class GroupAggregationAlgorithm(Algorithm):
    name = "group_aggregation"
    display_name = "分组聚合"
    version = "1.0.0"
    description = "按维度对住院人次/住院时长/费用/成本等指标做单维或多维组合聚合统计。"
    tags = ("aggregation", "olap", "spark-sql")

    param_specs = [
        ParamSpec("dimensions", "list", required=True,
                  description="分组维度 key 列表（如 ['age_group', 'gender']）"),
        ParamSpec("metrics", "list", required=False, default=["discharge_count"],
                  description="聚合指标 key 列表（默认 discharge_count）"),
        ParamSpec("filters", "list", required=False, default=[],
                  description="过滤条件列表 [{'field','op','value'|'values'}]"),
        ParamSpec("sort", "list", required=False, default=[],
                  description="排序规则 [{'field','order'}]，默认按首个指标降序"),
        ParamSpec("limit", "int", required=False, default=100,
                  min_value=1, max_value=1000, description="返回行数上限"),
    ]

    def _execute(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        from app.extensions import ext
        service = AggregationService(provider=None, df=ctx.dataframe,
                                     cache=None, settings=ext.settings)
        # 缓存接口对算法直调不生效（由 API 层统一缓存），此处传 None
        result = service.run(ctx.params, use_cache=False)
        return result, None, "分组聚合计算完成"
