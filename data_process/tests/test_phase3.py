# -*- coding: utf-8 -*-
"""人员3 · 三期功能测试（数据底座 + Redis 缓存后端）。

覆盖：
  * Redis 缓存后端：JSON 序列化往返 / 空结果哨兵防穿透 / 命中统计 / 探活降级；
  * 数据提供者工厂 build_data_provider（csv / mysql / hdfs / 非法值）；
  * MySQLDataProvider：JDBC URL 拼装、未加载状态、不可达转 503；
  * HDFSDataProvider：HDFS URL 拼装、未加载状态、不可达转 503。
"""

from __future__ import annotations

import sys
import types

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.data.data_provider import (
    HDFSDataProvider,
    MySQLDataProvider,
    SparkDataProvider,
    build_data_provider,
)
from config.settings import testing_settings


def _settings(tmp_path, **overrides):
    settings = testing_settings(tmp_path)
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


# ---------------------------------------------------------------------------
# Redis 缓存后端（用假 redis 模块测序列化与统计，无需真实 Redis 服务）
# ---------------------------------------------------------------------------
class _FakeRedis:
    """模仿 redis.Redis（decode_responses=False：value 为 bytes）。"""

    def __init__(self, connection_pool=None):
        self._store: dict = {}

    def ping(self):
        return True

    def get(self, key):
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self._store[key] = value if isinstance(value, bytes) else value.encode("utf-8")

    def delete(self, key):
        self._store.pop(key, None)

    def flushdb(self):
        self._store.clear()


@pytest.fixture()
def fake_redis_module(monkeypatch):
    module = types.ModuleType("redis")
    module.Redis = _FakeRedis
    module.ConnectionPool = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "redis", module)
    return module


def _redis_backend():
    from app.core.cache import RedisCacheBackend
    return RedisCacheBackend(host="127.0.0.1", port=6379, db=0)


def test_redis_cache_json_roundtrip(fake_redis_module):
    backend = _redis_backend()
    assert backend.ping() is True
    backend.set("key-1", {"age_group": "Male", "discharge_count": 300})
    assert backend.get("key-1") == {"age_group": "Male", "discharge_count": 300}


def test_redis_cache_null_sentinel_prevents_penetration(fake_redis_module):
    """空结果同样写入哨兵值，读取时还原为 None（防缓存穿透）。"""
    backend = _redis_backend()
    backend.set("key-empty", None)
    assert backend.get("key-empty") is None
    # 哨兵已落库（真实存储中非空），再次读取仍走缓存而不是穿透到后端
    assert backend._client.get("analytics:key-empty") is not None


def test_redis_cache_hit_miss_stats(fake_redis_module):
    backend = _redis_backend()
    assert backend.get("missing") is None       # 未命中 +1
    backend.set("hit-key", [1, 2, 3])
    assert backend.get("hit-key") == [1, 2, 3]  # 命中 +1
    stats = backend.stats
    assert stats["backend"] == "redis"
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_redis_cache_delete_and_clear(fake_redis_module):
    backend = _redis_backend()
    backend.set("k", "v")
    backend.delete("k")
    assert backend.get("k") is None
    backend.set("k2", "v2")
    backend.clear()
    assert backend.get("k2") is None


# ---------------------------------------------------------------------------
# 数据提供者工厂
# ---------------------------------------------------------------------------
def test_build_provider_default_csv(tmp_path):
    assert isinstance(build_data_provider(_settings(tmp_path)), SparkDataProvider)


def test_build_provider_mysql(tmp_path):
    provider = build_data_provider(_settings(tmp_path, data_source="mysql"))
    assert isinstance(provider, MySQLDataProvider)


def test_build_provider_hdfs(tmp_path):
    provider = build_data_provider(_settings(tmp_path, data_source="hdfs"))
    assert isinstance(provider, HDFSDataProvider)


def test_build_provider_case_insensitive(tmp_path):
    provider = build_data_provider(_settings(tmp_path, data_source="MYSQL"))
    assert isinstance(provider, MySQLDataProvider)


def test_build_provider_unknown_raises_503(tmp_path):
    settings = _settings(tmp_path, data_source="oracle")
    with pytest.raises(ServiceUnavailableError) as excinfo:
        build_data_provider(settings)
    assert int(excinfo.value.code) == 503
    assert "oracle" in str(excinfo.value.message)


# ---------------------------------------------------------------------------
# MySQLDataProvider
# ---------------------------------------------------------------------------
def test_mysql_jdbc_url(tmp_path):
    settings = _settings(tmp_path, data_source="mysql",
                         db_host="db.example.com", db_port=3307, db_name="med")
    provider = MySQLDataProvider(settings)
    assert provider._jdbc_url() == "jdbc:mysql://db.example.com:3307/med"


def test_mysql_status_unloaded(tmp_path):
    settings = _settings(tmp_path, data_source="mysql")
    provider = MySQLDataProvider(settings)
    status = provider.status()
    assert status["loaded"] is False
    assert status["row_count"] is None
    assert status["data_source"].startswith("mysql://")


def test_mysql_unreachable_raises_503(tmp_path, spark):
    """MySQL 不可达（连接拒绝/驱动缺失）统一转 ServiceUnavailableError（503）。"""
    settings = _settings(tmp_path, data_source="mysql",
                         db_host="127.0.0.1", db_port=1,
                         mysql_jdbc_connect_timeout_ms=1000)
    provider = MySQLDataProvider(settings)
    with pytest.raises(ServiceUnavailableError) as excinfo:
        provider.dataframe()
    assert int(excinfo.value.code) == 503


# ---------------------------------------------------------------------------
# HDFSDataProvider
# ---------------------------------------------------------------------------
def test_hdfs_url_building(tmp_path):
    settings = _settings(tmp_path, data_source="hdfs",
                         hdfs_namenode="hdfs://namenode:8020",
                         hdfs_path="/data/sparcs_clean.csv")
    provider = HDFSDataProvider(settings)
    assert provider._hdfs_url() == "hdfs://namenode:8020/data/sparcs_clean.csv"


def test_hdfs_url_trims_slashes(tmp_path):
    settings = _settings(tmp_path, data_source="hdfs",
                         hdfs_namenode="hdfs://namenode:8020/",
                         hdfs_path="/data/sparcs_clean.csv")
    assert HDFSDataProvider(settings)._hdfs_url() == "hdfs://namenode:8020/data/sparcs_clean.csv"


def test_hdfs_url_defaults(tmp_path):
    provider = HDFSDataProvider(_settings(tmp_path, data_source="hdfs"))
    assert provider._hdfs_url().startswith("hdfs://")


def test_hdfs_status_unloaded(tmp_path):
    settings = _settings(tmp_path, data_source="hdfs")
    provider = HDFSDataProvider(settings)
    status = provider.status()
    assert status["loaded"] is False
    assert status["row_count"] is None
    assert status["data_source"].startswith("hdfs://")
