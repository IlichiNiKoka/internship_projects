# -*- coding: utf-8 -*-
"""滑动窗口限流器（需求 3.3.5）：保护 API 免于突发流量。

进程内实现（单机部署够用），按客户端标识（API Token 或来源 IP）计数；
窗口内请求数超过上限时拒绝并返回 Retry-After 秒数（错误码 429）。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """滑动窗口限流：记录每个客户端最近 window_seconds 内的请求时间戳。"""

    def __init__(self, max_clients: int = 4096):
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._max_clients = max_clients

    def allow(self, key: str, max_requests: int, window_seconds: float) -> tuple[bool, float | None]:
        """判断客户端本次请求是否放行。

        返回 (放行?, retry_after)：
          * 放行时 retry_after 为 None；
          * 拒绝时 retry_after 为建议重试等待秒数。
        """
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and now - dq[0] >= window_seconds:
                dq.popleft()
            if len(dq) >= max_requests:
                retry_after = max(1.0, window_seconds - (now - dq[0]))
                return False, round(retry_after, 1)
            dq.append(now)
            self._evict_idle_clients(now, window_seconds)
            return True, None

    def _evict_idle_clients(self, now: float, window_seconds: float) -> None:
        """控制内存：客户端数超过上限时清理已空闲的客户端。"""
        if len(self._hits) <= self._max_clients:
            return
        for key in list(self._hits.keys()):
            dq = self._hits[key]
            while dq and now - dq[0] >= window_seconds:
                dq.popleft()
            if not dq:
                del self._hits[key]

    def reset(self) -> None:
        """清空全部计数（测试/运维用）。"""
        with self._lock:
            self._hits.clear()
