# -*- coding: utf-8 -*-
"""聚合分析请求 schema（3.3.1）。

只做结构与类型校验（字段名、必填、枚举、长度）；
维度/指标是否合法、过滤字段是否在白名单等业务校验在 AggregationService 完成，
两层校验分工明确、错误信息互不重叠。
"""

from __future__ import annotations

from marshmallow import Schema, ValidationError, fields, validate as ma_validate, validates_schema


class FilterSchema(Schema):
    """过滤条件：field + op + value（单值）或 values（列表）。"""
    field = fields.Str(required=True, error_messages={"required": "过滤条件缺少 field"})
    op = fields.Str(required=True, error_messages={"required": "过滤条件缺少 op"})
    value = fields.Raw(allow_none=True, load_default=None)
    values = fields.List(fields.Raw(), load_default=None)

    @validates_schema
    def _check_value_shape(self, data, **kwargs):
        op = data.get("op")
        if op in ("in", "not_in", "between") and not data.get("values"):
            raise ValidationError(f"操作符 {op} 需要 values 列表", "values")
        if op not in ("in", "not_in", "between") and data.get("value") is None:
            raise ValidationError(f"操作符 {op} 需要 value", "value")


class SortItemSchema(Schema):
    field = fields.Str(required=True)
    order = fields.Str(load_default="desc",
                       validate=ma_validate.OneOf(["asc", "desc"]))


class AggregationRequestSchema(Schema):
    dimensions = fields.List(fields.Str(), required=True,
                             validate=ma_validate.Length(min=1, max=5),
                             error_messages={"required": "缺少 dimensions 维度参数"})
    metrics = fields.List(fields.Str(), required=True,
                          error_messages={"required": "缺少 metrics 指标参数"})
    filters = fields.List(fields.Nested(FilterSchema), load_default=[])
    sort = fields.List(fields.Nested(SortItemSchema), load_default=[])
    limit = fields.Int(load_default=None, validate=ma_validate.Range(min=1, max=1000))
    # 二期 3.3.4：大结果集分页（page 从 1 开始；任一出现即启用分页模式）
    page = fields.Int(load_default=None, validate=ma_validate.Range(min=1))
    page_size = fields.Int(load_default=None, validate=ma_validate.Range(min=1, max=1000))


class BatchQuerySchema(Schema):
    """批量聚合中的单个子查询（过滤条件不在此声明，统一由批次级 filters 共享）。"""
    id = fields.Str(required=True, error_messages={"required": "批量子查询缺少 id"})
    dimensions = fields.List(fields.Str(), required=True,
                             validate=ma_validate.Length(min=1, max=5),
                             error_messages={"required": "缺少 dimensions 维度参数"})
    metrics = fields.List(fields.Str(), required=True,
                          error_messages={"required": "缺少 metrics 指标参数"})
    sort = fields.List(fields.Nested(SortItemSchema), load_default=[])
    limit = fields.Int(load_default=None, validate=ma_validate.Range(min=1, max=1000))


class AggregationBatchRequestSchema(Schema):
    """批量聚合请求：一次请求内共享同一组过滤条件执行多个分组聚合（大屏筛选联动优化）。

    结构：
      filters: list[FilterSchema]   批次级共享过滤条件（可选）
      queries: list[BatchQuerySchema] 子查询（1~20 个，id 需唯一）
    """
    filters = fields.List(fields.Nested(FilterSchema), load_default=[])
    queries = fields.List(fields.Nested(BatchQuerySchema), required=True,
                          validate=ma_validate.Length(min=1, max=20),
                          error_messages={"required": "批量聚合缺少 queries 子查询列表"})
