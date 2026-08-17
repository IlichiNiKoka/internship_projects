# -*- coding: utf-8 -*-
"""日志设施：控制台 + 按大小轮转文件，trace_id 注入（在请求上下文中可用）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from flask import g, has_request_context

from config.settings import Settings

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(trace_id)s | %(message)s"


class TraceIdFilter(logging.Filter):
    """把 Flask 请求上下文中的 trace_id 注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = getattr(g, "trace_id", "-") if has_request_context() else "-"
        return True


def configure_logging(settings: Settings) -> None:
    """初始化根日志器（幂等）。"""
    root = logging.getLogger()
    if getattr(root, "_analytics_configured", False):
        return
    root.setLevel(settings.log_level.upper())

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.log_dir / "analytics_service.log",
            maxBytes=10 * 1024 * 1024,   # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # 日志目录不可写时仅降级到控制台
        logging.getLogger(__name__).warning("日志目录不可写，仅输出到控制台: %s",
                                            settings.log_dir)

    root.addFilter(TraceIdFilter())
    root._analytics_configured = True  # type: ignore[attr-defined]
