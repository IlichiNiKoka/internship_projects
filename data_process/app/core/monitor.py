# -*- coding: utf-8 -*-
"""接口性能与错误监控（需求 3.3.4 用例“监控接口耗时” / 3.3.5 错误告警）。

  * 统计请求数 / 平均耗时 / 最大耗时 / 慢查询次数（超过阈值）；
  * 记录最近慢查询明细与按错误码聚合的错误计数；
  * 通过 GET /api/v1/meta/performance 对外暴露，支撑大屏监控与告警排查。
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque


class PerformanceMonitor:
    """进程级性能监控器（线程安全）。"""

    def __init__(self, slow_query_threshold_seconds: float = 5.0, recent_max: int = 50):
        self._slow_threshold = float(slow_query_threshold_seconds or 0.0)
        self._recent_max = recent_max
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._requests = 0
        self._errors = 0
        self._slow_count = 0
        self._total_seconds = 0.0
        self._max_seconds = 0.0
        self._recent_slow: deque[dict] = deque(maxlen=recent_max)
        self._error_by_code: Counter = Counter()

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def record_request(self, method: str, path: str, status_code: int, elapsed: float) -> None:
        """after_request 调用：累计请求与耗时，识别并记录慢查询。"""
        slow = elapsed >= self._slow_threshold
        with self._lock:
            self._requests += 1
            self._total_seconds += elapsed
            self._max_seconds = max(self._max_seconds, elapsed)
            if slow:
                self._slow_count += 1
                self._recent_slow.append({
                    "method": method,
                    "path": path,
                    "status": status_code,
                    "elapsed_seconds": round(elapsed, 3),
                })

    def record_error(self, code: int) -> None:
        """errorhandler 调用：错误计数（按错误码聚合）。"""
        with self._lock:
            self._errors += 1
            self._error_by_code[int(code)] += 1

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def stats(self) -> dict:
        with self._lock:
            uptime = time.monotonic() - self._started_at
            return {
                "uptime_seconds": round(uptime, 1),
                "total_requests": self._requests,
                "total_errors": self._errors,
                "avg_query_time_seconds": (
                    round(self._total_seconds / self._requests, 4) if self._requests else 0.0
                ),
                "max_query_time_seconds": round(self._max_seconds, 3),
                "slow_query_count": self._slow_count,
                "slow_query_threshold_seconds": self._slow_threshold,
                "recent_slow_queries": list(self._recent_slow),
                "errors_by_code": dict(self._error_by_code),
            }
