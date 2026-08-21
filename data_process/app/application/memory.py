# -*- coding: utf-8 -*-
"""多轮对话存储：进程内实现、Redis 实现与 LangChain 兼容适配器。"""

from __future__ import annotations

import asyncio
import copy
import functools
import json
import logging
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any

from app.application.models import (
    ConversationMessage,
    ConversationSession,
    new_id,
)
from app.core.exceptions import ConflictError, ServiceUnavailableError, TooManyRequestsError

logger = logging.getLogger(__name__)


class SessionConflictError(ConflictError):
    """会话已被其他请求更新，当前快照不能覆盖较新的数据。"""


class SessionLockLostError(ServiceUnavailableError):
    """分布式会话锁在操作完成前已丢失。"""


class SessionLockTimeoutError(TooManyRequestsError):
    """同一会话正在处理其他请求，等待锁超过配置阈值。"""


class _LocalLockSlot:
    def __init__(self):
        self.lock = threading.RLock()
        self.users = 0
        self.last_used = time.monotonic()


class _BoundedSessionLockPool:
    """引用计数的本地锁池；空闲锁按 LRU 回收，活动锁永不误删。"""

    def __init__(self, max_entries: int):
        self._max_entries = max(1, int(max_entries))
        self._slots: OrderedDict[str, _LocalLockSlot] = OrderedDict()
        self._guard = threading.Lock()

    @contextmanager
    def hold(self, key: str):
        with self._guard:
            slot = self._slots.get(key)
            if slot is None:
                slot = _LocalLockSlot()
                self._slots[key] = slot
            slot.users += 1
            slot.last_used = time.monotonic()
            self._slots.move_to_end(key)

        slot.lock.acquire()
        try:
            yield
        finally:
            slot.lock.release()
            with self._guard:
                slot.users -= 1
                slot.last_used = time.monotonic()
                self._slots.move_to_end(key)
                self._prune_unlocked()

    def discard(self, key: str) -> None:
        with self._guard:
            slot = self._slots.get(key)
            if slot is not None and slot.users == 0:
                self._slots.pop(key, None)

    def _prune_unlocked(self) -> None:
        if len(self._slots) <= self._max_entries:
            return
        for key, slot in list(self._slots.items()):
            if len(self._slots) <= self._max_entries:
                break
            if slot.users == 0:
                self._slots.pop(key, None)


class SessionStore(ABC):
    """会话持久化抽象，业务层不依赖具体 Redis 客户端。"""

    backend = "unknown"

    @abstractmethod
    def load(self, session_id: str) -> ConversationSession | None:
        raise NotImplementedError

    @abstractmethod
    def save(self, session: ConversationSession) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> dict[str, Any]:
        raise NotImplementedError

    @contextmanager
    def session_lock(self, session_id: str):
        """串行化同一会话的 load→修改→save；具体后端可提供分布式锁。"""
        yield


class InMemorySessionStore(SessionStore):
    """线程安全、带 TTL/LRU 上限的开发与测试会话存储。"""

    backend = "memory"

    def __init__(self, ttl_seconds: int = 86_400, max_sessions: int = 1_000):
        self._ttl = max(60, int(ttl_seconds))
        self._max_sessions = max(1, int(max_sessions))
        self._items: OrderedDict[str, tuple[float, ConversationSession]] = OrderedDict()
        self._lock = threading.RLock()
        self._session_locks = _BoundedSessionLockPool(self._max_sessions)

    def load(self, session_id: str) -> ConversationSession | None:
        with self._lock:
            item = self._items.get(session_id)
            if item is None:
                return None
            expires_at, session = item
            if expires_at <= time.monotonic():
                self._items.pop(session_id, None)
                self._session_locks.discard(session_id)
                return None
            self._items.move_to_end(session_id)
            return copy.deepcopy(session)

    def save(self, session: ConversationSession) -> None:
        with self._lock:
            self._purge_expired()
            self._items[session.id] = (
                time.monotonic() + self._ttl,
                copy.deepcopy(session),
            )
            self._items.move_to_end(session.id)
            while len(self._items) > self._max_sessions:
                evicted_id, _ = self._items.popitem(last=False)
                self._session_locks.discard(evicted_id)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            deleted = self._items.pop(session_id, None) is not None
        self._session_locks.discard(session_id)
        return deleted

    def health(self) -> dict[str, Any]:
        with self._lock:
            self._purge_expired()
            return {
                "backend": self.backend,
                "status": "ok",
                "session_count": len(self._items),
                "ttl_seconds": self._ttl,
            }

    @contextmanager
    def session_lock(self, session_id: str):
        with self._session_locks.hold(session_id):
            yield
        with self._lock:
            exists = session_id in self._items
        if not exists:
            self._session_locks.discard(session_id)

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [key for key, (deadline, _) in self._items.items() if deadline <= now]
        for key in expired:
            self._items.pop(key, None)
            self._session_locks.discard(key)


class RedisSessionStore(SessionStore):
    """JSON + TTL 的 Redis 会话存储。

    ``redis_client`` 可在测试中注入 fakeredis/替身；生产环境不注入时使用
    ``redis.Redis.from_url``。每次保存使用 SETEX 刷新会话 TTL。
    """

    backend = "redis"

    def __init__(
        self,
        redis_url: str,
        *,
        ttl_seconds: int = 86_400,
        key_prefix: str = "medical:conversation:",
        redis_client=None,
        socket_timeout: float = 1.0,
        lock_timeout: float = 900.0,
        lock_blocking_timeout: float = 10.0,
        lock_renew_interval: float | None = None,
        fallback_lock_max_entries: int = 1_024,
    ):
        self._ttl = max(60, int(ttl_seconds))
        self._prefix = key_prefix
        self._lock_timeout = max(5.0, float(lock_timeout))
        self._lock_blocking_timeout = max(0.0, float(lock_blocking_timeout))
        default_renew = min(30.0, self._lock_timeout / 3.0)
        requested_renew = default_renew if lock_renew_interval is None else float(lock_renew_interval)
        self._lock_renew_interval = max(
            1.0, min(requested_renew, self._lock_timeout * 0.8)
        )
        self._fallback_locks = _BoundedSessionLockPool(fallback_lock_max_entries)
        self._cas_locks = _BoundedSessionLockPool(fallback_lock_max_entries)
        self._lease_local = threading.local()
        if redis_client is not None:
            self._client = redis_client
        else:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - 由部署环境决定
                raise RuntimeError("使用 Redis 会话存储需要安装 redis 包") from exc
            self._client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=socket_timeout,
                socket_timeout=socket_timeout,
            )

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def load(self, session_id: str) -> ConversationSession | None:
        try:
            raw = self._client.get(self._key(session_id))
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            session = ConversationSession.from_dict(json.loads(raw))
            # 仅驻留于 Python 对象，用作下次保存的 CAS 期望版本，不写入 JSON。
            setattr(session, "_store_version", session.version)
            return session
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise ServiceUnavailableError("Redis 会话读取失败") from exc

    def save(self, session: ConversationSession) -> None:
        try:
            self._assert_lock_valid(session.id)
            expected_version = getattr(session, "_store_version", None)
            if expected_version is not None and session.version <= expected_version:
                # 即使调用方忘记 touch()，也保证每次写入都有严格递增的版本。
                session.version = int(expected_version) + 1
            payload = json.dumps(
                session.to_storage_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            key = self._key(session.id)
            pipeline_factory = getattr(self._client, "pipeline", None)
            if callable(pipeline_factory):
                self._save_with_cas_pipeline(
                    session.id,
                    key,
                    payload,
                    session.version,
                    expected_version,
                    pipeline_factory,
                )
            else:
                # 简易测试替身通常没有 pipeline；本地临界区仍执行版本检查。
                with self._cas_locks.hold(key):
                    raw = self._client.get(key)
                    self._validate_expected_version(raw, expected_version)
                    self._client.setex(key, self._ttl, payload)
            setattr(session, "_store_version", session.version)
        except (SessionConflictError, SessionLockLostError, ServiceUnavailableError):
            raise
        except Exception as exc:
            raise ServiceUnavailableError("Redis 会话保存失败") from exc

    def delete(self, session_id: str) -> bool:
        try:
            self._assert_lock_valid(session_id)
            key = self._key(session_id)
            deleted = bool(self._client.delete(key))
            self._fallback_locks.discard(session_id)
            self._cas_locks.discard(key)
            return deleted
        except (SessionLockLostError, ServiceUnavailableError):
            raise
        except Exception as exc:
            raise ServiceUnavailableError("Redis 会话删除失败") from exc

    def health(self) -> dict[str, Any]:
        try:
            ok = bool(self._client.ping())
            return {
                "backend": self.backend,
                "status": "ok" if ok else "degraded",
                "ttl_seconds": self._ttl,
                "lock_timeout_seconds": self._lock_timeout,
                "lock_renew_interval_seconds": self._lock_renew_interval,
            }
        except Exception as exc:  # 健康检查不能泄露连接串/凭证
            return {
                "backend": self.backend,
                "status": "unavailable",
                "error": type(exc).__name__,
                "ttl_seconds": self._ttl,
                "lock_timeout_seconds": self._lock_timeout,
                "lock_renew_interval_seconds": self._lock_renew_interval,
            }

    @staticmethod
    def _decode_stored_version(raw) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return int(data.get("version") or 0)

    def _validate_expected_version(self, raw, expected_version: int | None) -> None:
        current_version = self._decode_stored_version(raw)
        if expected_version is None:
            if current_version is not None:
                raise SessionConflictError("会话已存在，拒绝用未加载的快照覆盖")
            return
        if current_version != int(expected_version):
            raise SessionConflictError(
                f"会话版本冲突：期望 {expected_version}，实际 {current_version}"
            )

    def _save_with_cas_pipeline(
        self,
        session_id: str,
        key: str,
        payload: str,
        new_version: int,
        expected_version: int | None,
        pipeline_factory,
    ) -> None:
        pipe = pipeline_factory()
        try:
            pipe.watch(key)
            raw = pipe.get(key)
            self._validate_expected_version(raw, expected_version)
            self._assert_lock_valid(session_id)
            pipe.multi()
            pipe.setex(key, self._ttl, payload)
            pipe.execute()
        except Exception as exc:
            if type(exc).__name__ == "WatchError":
                raise SessionConflictError(
                    f"会话在保存版本 {new_version} 前已被其他请求更新"
                ) from exc
            raise
        finally:
            reset = getattr(pipe, "reset", None)
            if callable(reset):
                reset()

    def _assert_lock_valid(self, session_id: str) -> None:
        lease = getattr(self._lease_local, "lease", None)
        if not lease or lease["session_id"] != session_id:
            return
        if lease["lost"].is_set():
            raise SessionLockLostError("Redis 会话锁已丢失，拒绝保存旧快照")
        owned = getattr(lease["lock"], "owned", None)
        if callable(owned):
            try:
                is_owned = bool(owned())
            except Exception as exc:
                lease["lost"].set()
                raise SessionLockLostError("无法确认 Redis 会话锁所有权") from exc
            if not is_owned:
                lease["lost"].set()
                raise SessionLockLostError("Redis 会话锁已过期，拒绝保存旧快照")

    def _renew_lock(self, lease: dict[str, Any]) -> None:
        lock = lease["lock"]
        reacquire = getattr(lock, "reacquire", None)
        extend = getattr(lock, "extend", None)
        while not lease["stop"].wait(self._lock_renew_interval):
            try:
                if callable(reacquire):
                    renewed = reacquire()
                elif callable(extend):
                    renewed = extend(self._lock_timeout, replace_ttl=True)
                else:
                    # 非 redis-py 替身无法续租；CAS 仍可防止过期后的静默覆盖。
                    return
                if renewed is False:
                    raise SessionLockLostError("Redis 会话锁续租返回失败")
            except Exception:
                lease["lost"].set()
                logger.error("Redis 会话锁续租失败", exc_info=True)
                return

    @contextmanager
    def session_lock(self, session_id: str):
        """Redis 可用时用分布式锁；轻量测试替身无 lock() 时用本地锁。"""
        lock_factory = getattr(self._client, "lock", None)
        if callable(lock_factory):
            lock_kwargs = {
                "timeout": self._lock_timeout,
                "blocking_timeout": self._lock_blocking_timeout,
                # 续租线程需要读取同一 token，不能使用 redis-py 的线程局部 token。
                "thread_local": False,
            }
            try:
                lock = lock_factory(f"{self._key(session_id)}:lock", **lock_kwargs)
            except TypeError:
                lock_kwargs.pop("thread_local")
                lock = lock_factory(f"{self._key(session_id)}:lock", **lock_kwargs)
            try:
                acquired = lock.acquire(blocking=True)
            except Exception as exc:
                raise ServiceUnavailableError("Redis 会话锁获取失败") from exc
            if not acquired:
                raise SessionLockTimeoutError(
                    "同一会话正在处理其他请求，请稍后重试"
                )
            lease = {
                "session_id": session_id,
                "lock": lock,
                "lost": threading.Event(),
                "stop": threading.Event(),
            }
            previous_lease = getattr(self._lease_local, "lease", None)
            self._lease_local.lease = lease
            renew_thread = threading.Thread(
                target=self._renew_lock,
                args=(lease,),
                name="redis-session-lock-renewer",
                daemon=True,
            )
            renew_thread.start()
            try:
                yield
            finally:
                lease["stop"].set()
                renew_thread.join(timeout=min(1.0, self._lock_renew_interval))
                self._lease_local.lease = previous_lease
                try:
                    lock.release()
                except Exception:
                    logger.warning("Redis 会话锁释放失败", exc_info=True)
            return

        with self._fallback_locks.hold(session_id):
            yield
        self._fallback_locks.discard(session_id)


class LangChainSessionMemory:
    """把统一会话存储适配成 LangChain Memory 常用协议。

    适配器故意不继承特定 LangChain 版本的 ``BaseMemory``，避免 0.x/1.x
    包路径变化污染业务代码；它提供相同的 ``load_memory_variables``、
    ``save_context``、``clear`` 方法，并在安装 ``langchain-core`` 时返回标准
    HumanMessage/AIMessage，否则返回可 JSON 化的消息字典。
    """

    memory_key = "history"
    input_key = "input"
    output_key = "output"

    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        *,
        max_messages: int = 100,
    ):
        self._store = store
        self.session_id = session_id
        self._max_messages = max(2, int(max_messages))

    @property
    def memory_variables(self) -> list[str]:
        """LangChain BaseMemory 兼容属性。"""
        return [self.memory_key]

    @contextmanager
    def session_lock(self):
        """供编排层将 load→修改→save 放在同一个原子区间。"""
        with self._store.session_lock(self.session_id):
            yield

    def load_session(self) -> ConversationSession | None:
        """读取完整业务会话；调用方可在 ``session_lock`` 外层统一加锁。"""
        return self._store.load(self.session_id)

    def save_session(self, session: ConversationSession) -> None:
        """保存完整业务会话；本方法不重复获取可能非重入的 Redis 锁。"""
        if session.id != self.session_id:
            raise ValueError("不能通过当前 Memory 保存其他 session_id 的会话")
        self._store.save(session)

    def delete_session(self, *, acquire_lock: bool = True) -> bool:
        """删除完整业务会话；默认与聊天写入使用相同的会话锁。"""
        if not acquire_lock:
            return self._store.delete(self.session_id)
        with self._store.session_lock(self.session_id):
            return self._store.delete(self.session_id)

    def load_memory_variables(self, inputs: dict | None = None) -> dict[str, Any]:
        session = self._store.load(self.session_id)
        messages = session.messages if session else []
        try:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

            converted = []
            for message in messages:
                if message.role == "user":
                    converted.append(HumanMessage(content=message.content))
                elif message.role == "assistant":
                    converted.append(AIMessage(content=message.content))
                else:
                    converted.append(SystemMessage(content=message.content))
            return {self.memory_key: converted}
        except ImportError:
            return {
                self.memory_key: [
                    {"role": item.role, "content": item.content}
                    for item in messages
                ]
            }

    def save_context(self, inputs: dict[str, Any], outputs: dict[str, Any]) -> None:
        with self.session_lock():
            session = self.load_session() or ConversationSession(id=self.session_id)
            user_text = str(inputs.get(self.input_key) or inputs.get("query") or "")
            assistant_text = str(outputs.get(self.output_key) or outputs.get("text") or "")
            if user_text:
                session.messages.append(ConversationMessage(
                    id=new_id("msg"), role="user", content=user_text
                ))
            if assistant_text:
                session.messages.append(ConversationMessage(
                    id=new_id("msg"), role="assistant", content=assistant_text
                ))
            session.messages = session.messages[-self._max_messages:]
            session.touch()
            self.save_session(session)

    def clear(self) -> None:
        self.delete_session(acquire_lock=True)

    async def aload_memory_variables(
        self, inputs: dict | None = None
    ) -> dict[str, Any]:
        return await self._run_async(self.load_memory_variables, inputs)

    async def asave_context(
        self, inputs: dict[str, Any], outputs: dict[str, Any]
    ) -> None:
        await self._run_async(self.save_context, inputs, outputs)

    async def aclear(self) -> None:
        await self._run_async(self.clear)

    async def aload_session(self) -> ConversationSession | None:
        return await self._run_async(self.load_session)

    async def asave_session(self, session: ConversationSession) -> None:
        await self._run_async(self.save_session, session)

    async def adelete_session(self, *, acquire_lock: bool = True) -> bool:
        return await self._run_async(
            functools.partial(self.delete_session, acquire_lock=acquire_lock)
        )

    @staticmethod
    async def _run_async(func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(func, *args))


class ResilientSessionStore(SessionStore):
    """auto 模式使用：Redis 首次连接失败后切换到有界内存存储。"""

    backend = "auto"

    def __init__(self, primary: SessionStore, fallback: SessionStore):
        self._primary = primary
        self._fallback = fallback
        self._degraded = False
        self._guard = threading.RLock()
        self._pinned = threading.local()

    def _active(self) -> SessionStore:
        pinned = getattr(self._pinned, "store", None)
        if pinned is not None:
            return pinned
        with self._guard:
            return self._fallback if self._degraded else self._primary

    @staticmethod
    def _must_propagate(exc: Exception) -> bool:
        """业务冲突/锁竞争不是 Redis 宕机，不能切到另一份数据源。"""
        return isinstance(
            exc,
            (
                SessionConflictError,
                SessionLockLostError,
                SessionLockTimeoutError,
                TimeoutError,
                ValueError,
                TypeError,
            ),
        )

    def _mark_degraded(self) -> None:
        with self._guard:
            changed = not self._degraded
            self._degraded = True
        if changed:
            logger.warning("Redis 会话存储不可用，已切换到进程内存储", exc_info=True)

    def _mirror_after_transition(self, method: str, result, args):
        """处理切换瞬间已成功的 Redis 操作，避免数据只留在旧后端。"""
        with self._guard:
            degraded = self._degraded
        if not degraded:
            return result
        if method == "load":
            fallback_value = self._fallback.load(*args)
            if fallback_value is not None:
                return fallback_value
            if result is not None:
                self._fallback.save(result)
            return result
        if method == "save":
            self._fallback.save(*args)
            return result
        if method == "delete":
            return bool(self._fallback.delete(*args) or result)
        return result

    def _call(self, method: str, *args):
        store = self._active()
        try:
            result = getattr(store, method)(*args)
            if store is self._primary:
                return self._mirror_after_transition(method, result, args)
            return result
        except Exception as exc:
            if store is self._fallback or self._must_propagate(exc):
                raise
            self._mark_degraded()
            # 已经进入 primary 会话锁时不能在同一事务中途换后端。
            if getattr(self._pinned, "store", None) is self._primary:
                raise
            return getattr(self._fallback, method)(*args)

    def load(self, session_id: str) -> ConversationSession | None:
        return self._call("load", session_id)

    def save(self, session: ConversationSession) -> None:
        self._call("save", session)

    def delete(self, session_id: str) -> bool:
        return bool(self._call("delete", session_id))

    def health(self) -> dict[str, Any]:
        active = self._active()
        data = active.health()
        return {
            **data,
            "backend": self.backend,
            "active_backend": active.backend,
            "degraded": self._degraded,
        }

    @contextmanager
    def session_lock(self, session_id: str):
        store = self._active()
        try:
            manager = store.session_lock(session_id)
            manager.__enter__()
        except Exception as exc:
            if store is self._fallback or self._must_propagate(exc):
                raise
            self._mark_degraded()
            manager = self._fallback.session_lock(session_id)
            manager.__enter__()
            store = self._fallback
        previous_store = getattr(self._pinned, "store", None)
        self._pinned.store = store
        try:
            try:
                yield
            except BaseException:
                manager.__exit__(*sys.exc_info())
                raise
            else:
                manager.__exit__(None, None, None)
        finally:
            self._pinned.store = previous_store


def build_session_store(settings, redis_client=None) -> SessionStore:
    """按配置构建存储；默认无 Redis 地址时安全退化为进程内存储。"""
    backend = str(getattr(settings, "conversation_backend", "auto") or "auto").lower()
    redis_url = str(getattr(settings, "redis_url", "") or "").strip()
    ttl = int(getattr(settings, "conversation_ttl_seconds", 86_400))
    max_sessions = int(getattr(settings, "conversation_max_sessions", 1_000))
    lock_timeout = float(
        getattr(settings, "conversation_lock_timeout_seconds", 900.0)
    )
    lock_blocking_timeout = float(
        getattr(settings, "conversation_lock_blocking_timeout_seconds", 10.0)
    )
    configured_renew_interval = getattr(
        settings, "conversation_lock_renew_interval_seconds", None
    )
    lock_renew_interval = (
        None
        if configured_renew_interval in (None, "")
        else float(configured_renew_interval)
    )
    production = str(getattr(settings, "env", "development")).lower() == "production"

    if backend not in {"auto", "memory", "redis"}:
        raise ValueError(f"不支持的会话存储后端: {backend}")
    if production:
        if backend == "memory" or (not redis_url and redis_client is None):
            raise RuntimeError("生产环境必须配置 Redis 会话存储，避免多 worker 丢失对话")
        # 生产环境不允许 Redis 失败后静默切到单进程内存。
        if backend == "auto":
            backend = "redis"
    if backend == "memory" or (backend == "auto" and not redis_url and redis_client is None):
        return InMemorySessionStore(ttl_seconds=ttl, max_sessions=max_sessions)
    try:
        redis_store = RedisSessionStore(
            redis_url,
            ttl_seconds=ttl,
            key_prefix=str(getattr(settings, "redis_key_prefix", "medical:conversation:")),
            redis_client=redis_client,
            socket_timeout=float(getattr(settings, "redis_socket_timeout", 1.0)),
            lock_timeout=lock_timeout,
            lock_blocking_timeout=lock_blocking_timeout,
            lock_renew_interval=lock_renew_interval,
            fallback_lock_max_entries=max_sessions,
        )
        if backend == "auto":
            return ResilientSessionStore(
                redis_store,
                InMemorySessionStore(ttl_seconds=ttl, max_sessions=max_sessions),
            )
        return redis_store
    except Exception:
        if backend == "redis":
            raise
        logger.warning("Redis 会话存储初始化失败，降级到进程内存储", exc_info=True)
        return InMemorySessionStore(ttl_seconds=ttl, max_sessions=max_sessions)
