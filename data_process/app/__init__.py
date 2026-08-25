# -*- coding: utf-8 -*-
"""应用工厂：create_app 统一组装配置、扩展、算法注册、蓝本与中间件。

依赖注入设计：
  create_app(settings, data_provider) 允许测试注入小数据集替身，
  生产环境不传参时自动构建 SparkDataProvider。
"""

from __future__ import annotations

import logging

from flask import Flask

from app.core.cache import build_cache
from app.core.monitor import PerformanceMonitor
from app.core.ratelimiter import SlidingWindowLimiter
from app.utils.logging_conf import configure_logging
from config.settings import Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, data_provider=None) -> Flask:
    settings = settings or Settings.load()

    # 1) 日志
    configure_logging(settings)

    # 2) Flask 应用（JSON 输出中文不转义，便于阅读与联调）
    app = Flask(settings.app_name)
    app.json.ensure_ascii = False
    app.config["PROPAGATE_EXCEPTIONS"] = False

    # 3) 扩展容器
    from app.extensions import ext
    ext.settings = settings
    # 二期 3.3.4：in-memory / redis 可切换，Redis 不可用时自动降级进程内缓存
    ext.cache = build_cache(settings)
    # 二期 3.3.4 / 3.3.5：接口耗时监控与滑动窗口限流
    ext.monitor = PerformanceMonitor(
        slow_query_threshold_seconds=settings.slow_query_threshold_seconds)
    ext.rate_limiter = SlidingWindowLimiter()

    # 4) 数据源（按 data_source 配置选择 csv / mysql / hdfs；测试可注入替身）
    data_provider_was_injected = data_provider is not None
    if data_provider is None:
        from app.data.data_provider import build_data_provider
        data_provider = build_data_provider(settings)
    ext.data_provider = data_provider

    # 4.5) 启动预热：后台线程预加载数据源，首个用户请求不再承担冷启动开销。
    # DataProvider 实现内部有双重检查锁，与预热线程并发安全。
    # 仅对内部构建的真实数据源生效；测试注入的替身（内存 DataFrame）不预热，
    # 且 testing 环境一律跳过，避免测试被 Spark 启动拖慢。
    if (
        data_provider_was_injected is False
        and getattr(settings, "warmup_on_startup", False)
        and settings.env != "testing"
    ):
        import threading

        def _warmup() -> None:
            try:
                data_provider.dataframe()
                status = data_provider.status()
                logger.info("启动预热完成: %s 行，耗时 %ss",
                            status.get("row_count"), status.get("load_seconds"))
            except Exception as exc:  # noqa: BLE001 —— 预热失败不阻塞服务，首次请求再试
                logger.warning("启动预热失败（将在首次请求时重试）: %s", exc)

        threading.Thread(target=_warmup, daemon=True, name="data-warmup").start()

    # 5) 算法组件注册（幂等）
    from app.algorithms.base import register_builtin_algorithms
    register_builtin_algorithms()

    # 6) 蓝本与中间件
    from app.api.v1 import register_blueprints
    register_blueprints(app)

    from app.core.middleware import register_middlewares
    register_middlewares(app)

    logger.info("应用启动完成: %s (env=%s, version=%s)",
                settings.app_name, settings.env, settings.version)
    return app
