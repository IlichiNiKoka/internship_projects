# -*- coding: utf-8 -*-
"""多维度聚合分析服务测试（3.3.1）。

测试数据构造规则（见 conftest.py）：
  * 600 行；age_group = i % 5（每组 120）；gender = i % 2（每组 300）；
  * total_charges = 1000 + 10i，因此按 age_group 分组的平均值可精确计算。
"""

import pytest

from app.core.cache import InMemoryTTLCache
from app.core.exceptions import InvalidDimensionError, InvalidFilterError, InvalidMetricError
from app.services.aggregation_service import AggregationService

AGE_GROUPS = ["0 to 17", "18 to 29", "30 to 49", "50 to 69", "70 or Older"]


@pytest.fixture()
def service(sample_df):
    return AggregationService(df=sample_df)


def test_single_dimension_count(service):
    result = service.run({"dimensions": ["age_group"], "metrics": ["discharge_count"]})
    assert result["row_count"] == 5
    counts = {r["age_group"]: r["discharge_count"] for r in result["rows"]}
    assert counts == {g: 120 for g in AGE_GROUPS}


def test_multi_dimension_exact_avg(service):
    """平均值可精确断言：age_group=k 组 total_charges 均值 = 3975 + 10k。"""
    result = service.run({
        "dimensions": ["age_group", "gender"],
        "metrics": ["avg_total_charges"],
        "sort": [{"field": "age_group", "order": "asc"},
                 {"field": "gender", "order": "asc"}],
    })
    assert result["row_count"] == 10
    for r in result["rows"]:
        k = AGE_GROUPS.index(r["age_group"])
        assert r["avg_total_charges"] == 3975.0 + 10 * k


def test_filter_eq(service):
    result = service.run({
        "dimensions": ["age_group"],
        "metrics": ["discharge_count"],
        "filters": [{"field": "gender", "op": "eq", "value": "Male"}],
    })
    assert all(r["discharge_count"] == 60 for r in result["rows"])


def test_filter_numeric_between(service):
    result = service.run({
        "dimensions": ["age_group"],
        "metrics": ["discharge_count"],
        "filters": [{"field": "total_charges", "op": "between", "values": [2000, 3000]}],
    })
    # 2000 <= 1000+10i <= 3000 -> i in [100, 200]，共 101 行
    assert sum(r["discharge_count"] for r in result["rows"]) == 101


def test_sort_and_limit(service):
    result = service.run({
        "dimensions": ["hospital_county"],
        "metrics": ["discharge_count"],
        "sort": [{"field": "discharge_count", "order": "desc"}],
        "limit": 2,
    })
    assert result["row_count"] == 2
    assert result["rows"][0]["discharge_count"] >= result["rows"][1]["discharge_count"]


def test_cache_hit(sample_df):
    """相同口径的重复请求命中缓存，且命中计数正确。"""
    cache = InMemoryTTLCache(max_entries=10, ttl_seconds=60)
    svc = AggregationService(df=sample_df, cache=cache)
    first = svc.run({"dimensions": ["gender"], "metrics": ["discharge_count"]})
    assert first["cached"] is False
    second = svc.run({"dimensions": ["gender"], "metrics": ["discharge_count"]})
    assert second["cached"] is True
    assert cache.stats["hits"] == 1


def test_invalid_dimension(service):
    with pytest.raises(InvalidDimensionError):
        service.run({"dimensions": ["not_a_dim"], "metrics": ["discharge_count"]})


def test_invalid_metric(service):
    with pytest.raises(InvalidMetricError):
        service.run({"dimensions": ["age_group"], "metrics": ["not_a_metric"]})


def test_invalid_filter_field(service):
    with pytest.raises(InvalidFilterError):
        service.run({
            "dimensions": ["age_group"],
            "metrics": ["discharge_count"],
            "filters": [{"field": "hack_column", "op": "eq", "value": "x"}],
        })


def test_invalid_filter_op_for_string(service):
    with pytest.raises(InvalidFilterError):
        service.run({
            "dimensions": ["age_group"],
            "metrics": ["discharge_count"],
            "filters": [{"field": "gender", "op": "gte", "value": "M"}],
        })


def test_nan_normalized_to_null(service):
    """全空分组的 AVG 结果为 null（不是 NaN）。"""
    result = service.run({
        "dimensions": ["type_of_admission"],
        "metrics": ["avg_birth_weight"],
    })
    for r in result["rows"]:
        assert r["avg_birth_weight"] is None or isinstance(r["avg_birth_weight"], (int, float))
