# -*- coding: utf-8 -*-
"""配置从环境变量加载的类型解析测试。

回归保障：config/settings.py 顶部使用 `from __future__ import annotations`，
若 Settings.load() 直接用 f.type 判断类型会全部失效（注解变成字符串），
导致 bool/int/float/Path 字段被当成 str（真实运行时报
"'str' object has no attribute 'exists'"）。本用例锁死正确行为。
"""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings


def test_load_parses_env_types(monkeypatch):
    """环境变量应被解析为正确类型（bool/int/float/Path/str）。"""
    monkeypatch.setenv("ANALYTICS_CACHE_ENABLED", "false")
    monkeypatch.setenv("ANALYTICS_AGG_MAX_LIMIT", "1234")
    monkeypatch.setenv("ANALYTICS_SLOW_QUERY_THRESHOLD_SECONDS", "2.5")
    monkeypatch.setenv("ANALYTICS_DATA_CSV_PATH", "C:/data/clean.csv")
    monkeypatch.setenv("ANALYTICS_CACHE_BACKEND", "redis")

    s = Settings.load()
    assert s.cache_enabled is False
    assert s.agg_max_limit == 1234
    assert s.slow_query_threshold_seconds == 2.5
    assert isinstance(s.data_csv_path, Path)
    assert s.cache_backend == "redis"


def test_load_keeps_defaults_without_env(monkeypatch):
    """无环境变量时使用类型化默认值。"""
    for name in ("CACHE_ENABLED", "AGG_MAX_LIMIT", "SLOW_QUERY_THRESHOLD_SECONDS",
                 "DATA_CSV_PATH", "CACHE_BACKEND"):
        monkeypatch.delenv(f"ANALYTICS_{name}", raising=False)

    s = Settings.load()
    assert s.cache_enabled is True
    assert isinstance(s.data_csv_path, Path)
