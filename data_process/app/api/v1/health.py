# -*- coding: utf-8 -*-
"""健康检查 API：服务与数据源就绪状态。"""

from __future__ import annotations

import time

from flask import Blueprint

from app.core.response import success
from app.extensions import ext

bp = Blueprint("health", __name__, url_prefix="/api/v1")

_STARTED_AT = time.monotonic()


@bp.get("/health")
def health():
    """服务健康状态（AI 模块与大屏可用作探活）。"""
    provider_status = ext.data_provider.status() if ext.data_provider else {}
    return success({
        "status": "ok",
        "service": ext.settings.app_name,
        "version": ext.settings.version,
        "env": ext.settings.env,
        "uptime_seconds": round(time.monotonic() - _STARTED_AT, 1),
        "data": provider_status,
        "cache": ext.cache.stats if ext.cache else {},
    })
