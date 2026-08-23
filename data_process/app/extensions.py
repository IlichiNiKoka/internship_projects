# -*- coding: utf-8 -*-
"""应用扩展容器：进程级单例存放处（DataProvider / 缓存 / 配置）。

使用简单命名空间而非 flask extensions 包，避免引入重依赖，
同时保持依赖注入能力：create_app(settings, data_provider=...) 可注入替身。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注用，避免循环导入
    from app.core.cache import CacheBackend
    from app.core.monitor import PerformanceMonitor
    from app.core.ratelimiter import SlidingWindowLimiter
    from app.data.data_provider import DataProvider
    from config.settings import Settings


class _Extensions:
    settings: "Settings | None" = None
    data_provider: "DataProvider | None" = None
    cache: "CacheBackend | None" = None
    monitor: "PerformanceMonitor | None" = None          # 二期：接口耗时/慢查询/错误监控
    rate_limiter: "SlidingWindowLimiter | None" = None   # 二期：滑动窗口限流


ext = _Extensions()
