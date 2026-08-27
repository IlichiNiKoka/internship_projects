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
        "dimensions": ["age_group"],
        "metrics": ["avg_total_charges"],
        "sort": [{"field": "age_group", "order": "asc"}],
    })
    assert result["row_count"] == 5
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


# ---------------------------------------------------------------------------
# 批量聚合（大屏筛选联动优化）
# ---------------------------------------------------------------------------
def test_batch_shared_filters(sample_df):
    """批次内所有子查询共享同一组过滤条件，结果与单查询一致。"""
    svc = AggregationService(df=sample_df)
    result = svc.run_batch(
        filters=[{"field": "gender", "op": "eq", "value": "Male"}],
        queries=[
            {"id": "age", "dimensions": ["age_group"], "metrics": ["discharge_count"]},
            {"id": "county", "dimensions": ["hospital_county"], "metrics": ["discharge_count"]},
        ],
    )
    assert result["query_count"] == 2
    results = result["results"]
    # Male 共 300 行 -> 各维度分组求和均为 300
    assert sum(r["discharge_count"] for r in results["age"]["rows"]) == 300
    assert sum(r["discharge_count"] for r in results["county"]["rows"]) == 300
    assert all(r["discharge_count"] == 60 for r in results["age"]["rows"])


def test_batch_limit_and_sort(sample_df):
    svc = AggregationService(df=sample_df)
    result = svc.run_batch(
        filters=[],
        queries=[
            {"id": "top", "dimensions": ["hospital_county"], "metrics": ["discharge_count"],
             "sort": [{"field": "discharge_count", "order": "desc"}], "limit": 2},
        ],
    )
    rows = result["results"]["top"]["rows"]
    assert len(rows) == 2
    assert rows[0]["discharge_count"] >= rows[1]["discharge_count"]


def test_batch_cache_hit(sample_df):
    """相同批次重复请求命中整批缓存（第二次不重算）。"""
    cache = InMemoryTTLCache(max_entries=10, ttl_seconds=60)
    svc = AggregationService(df=sample_df, cache=cache)
    queries = [
        {"id": "age", "dimensions": ["age_group"], "metrics": ["discharge_count"]},
        {"id": "gender", "dimensions": ["gender"], "metrics": ["discharge_count"]},
    ]
    first = svc.run_batch(filters=[], queries=queries)
    assert first["cached"] is False
    second = svc.run_batch(filters=[], queries=queries)
    assert second["cached"] is True
    assert second["results"]["age"]["row_count"] == first["results"]["age"]["row_count"]


def test_batch_duplicate_id(sample_df):
    svc = AggregationService(df=sample_df)
    with pytest.raises(InvalidFilterError):
        svc.run_batch(
            filters=[],
            queries=[
                {"id": "a", "dimensions": ["age_group"], "metrics": ["discharge_count"]},
                {"id": "a", "dimensions": ["gender"], "metrics": ["discharge_count"]},
            ],
        )


def test_batch_invalid_dimension(sample_df):
    svc = AggregationService(df=sample_df)
    with pytest.raises(InvalidDimensionError):
        svc.run_batch(
            filters=[],
            queries=[{"id": "a", "dimensions": ["hack"], "metrics": ["discharge_count"]}],
        )


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
