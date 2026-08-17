# -*- coding: utf-8 -*-
"""结果缓存抽象（一期：进程内 TTL+LRU；二期：无缝切换 Redis）。

需求 3.3.1 流程图中“缓存查询结果”为执行聚合计算的扩展用例。
一期以进程内缓存实现同一接口，二期仅需新增 RedisCacheBackend 并
改配置 ANALYTICS_CACHE_BACKEND=redis，业务代码零改动。
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

# 高频查询“空结果”也写入缓存的哨兵，避免穿透
_NULL_SENTINEL = "__ANALYTICS_NULL__"


class CacheBackend(ABC):
    """缓存后端统一接口。"""

    @abstractmethod
    def get(self, key: str) -> Any: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    @property
    @abstractmethod
    def stats(self) -> dict: ...


class NullCache(CacheBackend):
    """禁用缓存时的空实现（测试环境默认使用）。"""

    def get(self, key: str) -> Any:
        return None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        return None

    def delete(self, key: str) -> None:
        return None

    def clear(self) -> None:
        return None

    @property
    def stats(self) -> dict:
        return {"enabled": False, "size": 0, "max_entries": 0, "hits": 0, "misses": 0}


class InMemoryTTLCache(CacheBackend):
    """线程安全的进程内 TTL + LRU 缓存。"""

    def __init__(self, max_entries: int = 256, ttl_seconds: int = 300):
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (expire_at, _) in self._data.items() if expire_at <= now]
        for k in expired:
            del self._data[k]

    def get(self, key: str) -> Any:
        with self._lock:
            self._purge_expired()
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            expire_at, value = item
            if expire_at <= time.monotonic():
                del self._data[key]
                self._misses += 1
                return None
            # LRU：命中后移到末尾
            self._data.move_to_end(key)
            self._hits += 1
            return _NULL_SENTINEL if value == _NULL_SENTINEL else value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        with self._lock:
            self._purge_expired()
            if value is None:
                value = _NULL_SENTINEL
            self._data[key] = (time.monotonic() + (ttl_seconds or self._ttl), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:   # 超出容量淘汰最旧
                self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    @property
    def stats(self) -> dict:
        with self._lock:
            self._purge_expired()
            return {
                "enabled": True,
                "backend": "in-memory-ttl-lru",
                "size": len(self._data),
                "max_entries": self._max_entries,
                "ttl_seconds": self._ttl,
                "hits": self._hits,
                "misses": self._misses,
            }
