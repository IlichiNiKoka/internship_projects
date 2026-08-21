# -*- coding: utf-8 -*-
"""应用工厂：create_app 统一组装配置、扩展、算法注册、蓝本与中间件。

依赖注入设计：
  create_app(settings, data_provider) 允许测试注入小数据集替身，
  生产环境不传参时自动构建 SparkDataProvider。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask
    from config.settings import Settings

logger = logging.getLogger(__name__)


def create_app(settings: "Settings | None" = None, data_provider=None) -> "Flask":
    # 延迟导入 Web/配置依赖，使意图、工具、会话等纯业务模块可在无 Flask 的
    # 离线单元测试中独立导入。
    from flask import Flask
    from app.core.cache import InMemoryTTLCache, NullCache
    from app.utils.logging_conf import configure_logging
    from config.settings import Settings

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
    ext.cache = (
        InMemoryTTLCache(max_entries=settings.cache_max_entries,
                         ttl_seconds=settings.cache_ttl_seconds)
        if settings.cache_enabled else NullCache()
    )

    # 4) 数据源（默认 Spark CSV；测试可注入替身）
    if data_provider is None:
        from app.data.data_provider import SparkDataProvider
        data_provider = SparkDataProvider(settings)
    ext.data_provider = data_provider
    # 人员1应用服务按首次 /assistant 请求惰性构建；每个 Flask app 重置，
    # 避免测试/多应用场景复用旧 settings 或 data_provider。
    ext.application_service = None

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
