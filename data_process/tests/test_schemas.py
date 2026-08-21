# -*- coding: utf-8 -*-
"""请求 schema 校验测试。"""

import pytest
from marshmallow import ValidationError

from app.schemas.aggregation import AggregationRequestSchema, FilterSchema


def test_valid_request():
    data = AggregationRequestSchema().load({
        "dimensions": ["age_group"],
        "metrics": ["discharge_count"],
        "filters": [{"field": "gender", "op": "in", "values": ["Male"]}],
        "sort": [{"field": "discharge_count", "order": "desc"}],
        "limit": 10,
    })
    assert data["dimensions"] == ["age_group"]


def test_missing_dimensions():
    with pytest.raises(ValidationError):
        AggregationRequestSchema().load({"metrics": ["discharge_count"]})


def test_filter_in_needs_values():
    with pytest.raises(ValidationError):
        FilterSchema().load({"field": "gender", "op": "in"})


def test_filter_eq_needs_value():
    with pytest.raises(ValidationError):
        FilterSchema().load({"field": "gender", "op": "eq"})


def test_bad_sort_order():
    with pytest.raises(ValidationError):
        AggregationRequestSchema().load({
            "dimensions": ["age_group"],
            "metrics": ["discharge_count"],
            "sort": [{"field": "discharge_count", "order": "sideways"}],
        })
