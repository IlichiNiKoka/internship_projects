# -*- coding: utf-8 -*-
"""结果缓存抽象（一期：进程内 TTL+LRU；二期：无缝切换 Redis）。

需求 3.3.1 流程图中“缓存查询结果”为执行聚合计算的扩展用例，
需求 3.3.4 要求高频查询命中 Redis 缓存以降低响应时间。

二期新增 RedisCacheBackend 与 build_cache 工厂：
  * ANALYTICS_CACHE_BACKEND=redis 时优先使用 Redis（跨进程共享）；
  * Redis 未安装/连接失败时自动降级为进程内缓存（缓存可降级原则），
    业务代码零改动。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

# 高频查询“空结果”也写入缓存的哨兵，避免穿透
_NULL_SENTINEL = "__ANALYTICS_NULL__"


class CacheBackend(ABC):
    """缓存后端统一接口。"""

    @abstractmethod
    def get(self, key: str) -> Any: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...

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

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
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


class RedisCacheBackend(CacheBackend):
    """Redis 缓存后端（二期 3.3.4）：跨进程共享高频聚合查询结果。

    设计要点：
      * 值以 JSON 序列化存储，空结果同样以哨兵值缓存防穿透；
      * 连接池复用（ConnectionPool），避免每请求建连开销；
      * 任何 Redis 异常降级为“未命中/跳过写入”并记录日志，
        保证缓存故障不影响主流程（缓存可降级原则）；
      * key 统一加 analytics: 前缀，避免与其它系统冲突。
    """

    _KEY_PREFIX = "analytics:"

    def __init__(self, host: str = "127.0.0.1", port: int = 6379, db: int = 0,
                 password: str = "", connect_timeout: float = 2.0,
                 socket_timeout: float = 2.0, max_connections: int = 10,
                 default_ttl_seconds: int = 300):
        import redis  # 延迟导入：redis 为可选依赖，未安装时由 build_cache 降级
        self._default_ttl = default_ttl_seconds
        kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "db": int(db),
            "connect_timeout": float(connect_timeout),
            "socket_timeout": float(socket_timeout),
            "decode_responses": False,
        }
        if password:
            kwargs["password"] = password
        pool = redis.ConnectionPool(max_connections=max_connections, **kwargs)
        self._client = redis.Redis(connection_pool=pool)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._connected = False

    def ping(self) -> bool:
        """探活：成功返回 True 并更新连接状态（build_cache 用它做降级决策）。"""
        try:
            self._connected = bool(self._client.ping())
        except Exception as exc:  # noqa: BLE001 —— Redis 任何异常都按不可用处理
            logger.warning("Redis ping 失败: %s", exc)
            self._connected = False
        return self._connected

    # ------------------------------------------------------------------
    def get(self, key: str) -> Any:
        try:
            raw = self._client.get(self._KEY_PREFIX + key)
        except Exception as exc:  # noqa: BLE001 —— 故障降级为未命中
            logger.warning("Redis get 失败（按未命中降级）: %s", exc)
            raw = None
        if raw is None:
            with self._lock:
                self._misses += 1
            return None
        with self._lock:
            self._hits += 1
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Redis 缓存值反序列化失败: %s", key)
            return None
        return None if value == _NULL_SENTINEL else value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        if value is None:
            value = _NULL_SENTINEL
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
            self._client.setex(self._KEY_PREFIX + key,
                               int(ttl_seconds or self._default_ttl), payload)
        except Exception as exc:  # noqa: BLE001 —— 写入失败跳过，不阻塞主流程
            logger.warning("Redis set 失败（缓存写入降级跳过）: %s", exc)

    def delete(self, key: str) -> None:
        try:
            self._client.delete(self._KEY_PREFIX + key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis delete 失败: %s", exc)

    def clear(self) -> None:
        try:
            self._client.flushdb()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis flushdb 失败: %s", exc)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "backend": "redis",
                "connected": self._connected,
                "hits": self._hits,
                "misses": self._misses,
            }


def build_cache(settings) -> CacheBackend:
    """按配置构建缓存后端（二期 3.3.4）。

    * cache_enabled=False            -> NullCache；
    * cache_backend=redis 且连接成功 -> RedisCacheBackend；
    * 其余（未选 Redis / 未安装 redis 包 / 连接失败）-> 进程内 TTL 缓存。
    """
    if not settings.cache_enabled:
        return NullCache()

    if getattr(settings, "cache_backend", "in-memory") == "redis":
        try:
            backend = RedisCacheBackend(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password,
                connect_timeout=settings.redis_connect_timeout,
                default_ttl_seconds=settings.cache_ttl_seconds,
            )
            if backend.ping():
                logger.info("缓存后端: Redis(%s:%s/%s)",
                            settings.redis_host, settings.redis_port, settings.redis_db)
                return backend
            logger.warning("Redis 连接失败，降级为进程内缓存")
        except Exception as exc:  # noqa: BLE001 —— 未安装 redis 包等
            logger.warning("Redis 缓存后端不可用（%s），降级为进程内缓存", exc)

    return InMemoryTTLCache(max_entries=settings.cache_max_entries,
                            ttl_seconds=settings.cache_ttl_seconds)
