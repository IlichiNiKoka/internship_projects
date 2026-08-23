# -*- coding: utf-8 -*-
"""二期功能测试（3.3.4 API 性能优化 / 3.3.5 API 异常处理机制）。

覆盖：
  * 超时控制（run_with_timeout / 聚合 504 降级）；
  * 大结果集分页（page/page_size + pagination 元数据 + 旧模式兼容）；
  * Redis 缓存后端不可用时自动降级；
  * 权限控制（401 / 403 / 公开路径白名单）；
  * 限流（滑动窗口 429 + Retry-After）；
  * 413 请求体过大标准化（二期修复）；
  * 接口性能监控端点与慢查询/错误统计。

每个测试独立构建应用实例（依赖注入 MemoryDataProvider），互不影响。
"""

from __future__ import annotations

import time

import pytest

from app import create_app
from app.core.cache import InMemoryTTLCache, build_cache
from app.core.exceptions import ComputationTimeoutError
from app.core.ratelimiter import SlidingWindowLimiter
from app.core.timeout import run_with_timeout
from app.data.data_provider import MemoryDataProvider
from app.services.aggregation_service import AggregationService
from config.settings import testing_settings

STANDARD_KEYS = {"code", "message", "data", "query_time", "trace_id"}

AGG_PAYLOAD = {"dimensions": ["age_group"], "metrics": ["discharge_count"]}

PAGE_PAYLOAD = {
    "dimensions": ["age_group"],
    "metrics": ["discharge_count"],
    "sort": [{"field": "age_group", "order": "asc"}],
}


# ---------------------------------------------------------------------------
# 应用构建辅助
# ---------------------------------------------------------------------------
def _make_app(sample_df, tmp_path, **overrides):
    settings = testing_settings(tmp_path)
    for key, value in overrides.items():
        setattr(settings, key, value)
    provider = MemoryDataProvider(sample_df, row_count=600)
    return create_app(settings, data_provider=provider)


@pytest.fixture()
def secured_app(sample_df, tmp_path):
    """启用 API 认证与限流的应用实例。"""
    return _make_app(
        sample_df, tmp_path,
        api_auth_enabled=True,
        api_auth_tokens="token-a, token-b",
        api_auth_public_paths="/api/v1/health",
        rate_limit_enabled=True,
        rate_limit_requests=3,
        rate_limit_window_seconds=60,
    )


@pytest.fixture()
def secured_client(secured_app):
    return secured_app.test_client()


# ---------------------------------------------------------------------------
# 3.3.4 超时控制
# ---------------------------------------------------------------------------
def test_run_with_timeout_returns_result():
    assert run_with_timeout(lambda: 42, timeout_seconds=5.0) == 42


def test_run_with_timeout_no_limit():
    assert run_with_timeout(lambda: 7, timeout_seconds=None) == 7
    assert run_with_timeout(lambda: 7, timeout_seconds=0) == 7


def test_run_with_timeout_raises_504():
    with pytest.raises(ComputationTimeoutError) as excinfo:
        run_with_timeout(lambda: time.sleep(0.2), timeout_seconds=0.01)
    assert int(excinfo.value.code) == 504
    assert isinstance(excinfo.value.detail, dict)
    assert excinfo.value.detail["timeout_seconds"] == 0.01


def test_run_with_timeout_propagates_exception():
    def _boom():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run_with_timeout(_boom, timeout_seconds=5.0)


def test_aggregation_service_timeout(sample_df):
    service = AggregationService(df=sample_df, timeout_seconds=1e-9)
    with pytest.raises(ComputationTimeoutError):
        service.run(dict(AGG_PAYLOAD))


def test_aggregation_http_timeout_504(sample_df, tmp_path):
    app = _make_app(sample_df, tmp_path, agg_timeout_seconds=1e-9)
    client = app.test_client()
    resp = client.post("/api/v1/aggregations/run", json=AGG_PAYLOAD)
    body = resp.get_json()
    assert resp.status_code == 504
    assert body["code"] == 504
    assert STANDARD_KEYS <= set(body.keys())
    assert "detail" in body["data"]


# ---------------------------------------------------------------------------
# 3.3.4 大结果集分页
# ---------------------------------------------------------------------------
def test_aggregation_pagination_page2(sample_df, tmp_path):
    app = _make_app(sample_df, tmp_path)
    client = app.test_client()
    resp = client.post("/api/v1/aggregations/run",
                       json={**PAGE_PAYLOAD, "page": 2, "page_size": 2})
    body = resp.get_json()
    assert resp.status_code == 200
    data = body["data"]
    assert data["pagination"] == {
        "page": 2, "page_size": 2, "returned": 2, "has_more": True,
    }
    assert [row["age_group"] for row in data["rows"]] == ["30 to 49", "50 to 69"]
    assert data["truncated"] is True


def test_aggregation_pagination_last_page(sample_df, tmp_path):
    app = _make_app(sample_df, tmp_path)
    client = app.test_client()
    resp = client.post("/api/v1/aggregations/run",
                       json={**PAGE_PAYLOAD, "page": 3, "page_size": 2})
    data = resp.get_json()["data"]
    assert data["pagination"]["returned"] == 1
    assert data["pagination"]["has_more"] is False
    assert data["rows"][0]["age_group"] == "70 or Older"
    assert data["truncated"] is False


def test_aggregation_page_only_defaults_page_size(sample_df, tmp_path):
    app = _make_app(sample_df, tmp_path)
    client = app.test_client()
    resp = client.post("/api/v1/aggregations/run", json={**PAGE_PAYLOAD, "page": 1})
    data = resp.get_json()["data"]
    assert data["pagination"]["page"] == 1
    assert data["pagination"]["returned"] == 5
    assert data["pagination"]["has_more"] is False


def test_aggregation_legacy_mode_unchanged(sample_df, tmp_path):
    """未提供分页参数时保持一期行为：无 pagination 字段。"""
    app = _make_app(sample_df, tmp_path)
    client = app.test_client()
    resp = client.post("/api/v1/aggregations/run", json=AGG_PAYLOAD)
    data = resp.get_json()["data"]
    assert "pagination" not in data
    assert data["row_count"] == 5
    assert data["truncated"] is False


# ---------------------------------------------------------------------------
# 3.3.4 Redis 缓存后端降级
# ---------------------------------------------------------------------------
def test_cache_backend_default_inmemory(sample_df, tmp_path):
    settings = testing_settings(tmp_path)
    settings.cache_enabled = True
    backend = build_cache(settings)
    assert isinstance(backend, InMemoryTTLCache)


def test_cache_backend_redis_fallback(sample_df, tmp_path):
    """Redis 未安装或连接失败（无人监听的端口）时自动降级进程内缓存。"""
    settings = testing_settings(tmp_path)
    settings.cache_enabled = True
    settings.cache_backend = "redis"
    settings.redis_host = "127.0.0.1"
    settings.redis_port = 1
    settings.redis_connect_timeout = 0.5
    backend = build_cache(settings)
    assert isinstance(backend, InMemoryTTLCache)


# ---------------------------------------------------------------------------
# 3.3.5 权限控制
# ---------------------------------------------------------------------------
def test_auth_missing_token_401(secured_client):
    resp = secured_client.post("/api/v1/aggregations/run", json=AGG_PAYLOAD)
    body = resp.get_json()
    assert resp.status_code == 401
    assert body["code"] == 401
    assert STANDARD_KEYS <= set(body.keys())


def test_auth_invalid_token_403(secured_client):
    resp = secured_client.post("/api/v1/aggregations/run", json=AGG_PAYLOAD,
                               headers={"X-API-Key": "wrong-token"})
    body = resp.get_json()
    assert resp.status_code == 403
    assert body["code"] == 403


def test_auth_valid_token_200(secured_client):
    resp = secured_client.post("/api/v1/aggregations/run", json=AGG_PAYLOAD,
                               headers={"Authorization": "Bearer token-a"})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["code"] == 200
    assert body["data"]["row_count"] == 5


def test_auth_public_path_bypass(secured_client):
    resp = secured_client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.get_json()["code"] == 200


# ---------------------------------------------------------------------------
# 3.3.5 限流
# ---------------------------------------------------------------------------
def test_sliding_window_limiter_unit():
    limiter = SlidingWindowLimiter()
    assert limiter.allow("client-1", 2, 60) == (True, None)
    assert limiter.allow("client-1", 2, 60) == (True, None)
    allowed, retry_after = limiter.allow("client-1", 2, 60)
    assert allowed is False
    assert retry_after is not None and retry_after >= 1
    limiter.reset()
    assert limiter.allow("client-1", 2, 60) == (True, None)


def test_rate_limit_429_with_retry_after(secured_client):
    headers = {"X-API-Key": "token-a"}
    for _ in range(3):
        resp = secured_client.post("/api/v1/aggregations/run",
                                   json=AGG_PAYLOAD, headers=headers)
        assert resp.status_code == 200
    resp = secured_client.post("/api/v1/aggregations/run",
                               json=AGG_PAYLOAD, headers=headers)
    body = resp.get_json()
    assert resp.status_code == 429
    assert body["code"] == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------------------
# 3.3.5 请求体过大 413（二期修复：此前会因缺枚举成员变成 500）
# ---------------------------------------------------------------------------
def test_request_entity_too_large_413(sample_df, tmp_path):
    app = _make_app(sample_df, tmp_path)
    client = app.test_client()
    big_body = "x" * (2 * 1024 * 1024 + 16)
    resp = client.post("/api/v1/aggregations/run", data=big_body,
                       content_type="application/json")
    body = resp.get_json()
    assert resp.status_code == 413
    assert body["code"] == 413
    assert STANDARD_KEYS <= set(body.keys())


# ---------------------------------------------------------------------------
# 3.3.4 接口性能监控
# ---------------------------------------------------------------------------
def test_meta_performance_monitoring(sample_df, tmp_path):
    app = _make_app(sample_df, tmp_path, slow_query_threshold_seconds=0.0)
    client = app.test_client()

    resp = client.get("/api/v1/meta/performance")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["code"] == 200
    stats = body["data"]
    for key in ("total_requests", "total_errors", "avg_query_time_seconds",
                "max_query_time_seconds", "slow_query_count", "errors_by_code",
                "recent_slow_queries", "cache"):
        assert key in stats

    # 制造一个 404 错误，验证错误计数与慢查询记录
    client.get("/api/v1/no_such_route")
    stats2 = client.get("/api/v1/meta/performance").get_json()["data"]
    assert stats2["total_errors"] >= 1
    # 经 JSON 序列化后错误码键为字符串
    assert stats2["errors_by_code"].get("404", 0) >= 1
    assert stats2["slow_query_count"] >= 1
    assert len(stats2["recent_slow_queries"]) >= 1
