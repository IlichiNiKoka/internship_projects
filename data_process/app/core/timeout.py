# -*- coding: utf-8 -*-
"""超时控制（需求 3.3.4 / 3.3.5）：阻塞式 Spark 计算的限时执行与超时降级。

PySpark 的 collect()/count() 为阻塞调用，无法在调用线程内安全中断，
因此采用“后台线程执行 + 调用线程限时等待”：

  * 未超时：透传计算结果（或原样抛出执行异常）；
  * 超时：抛出 ComputationTimeoutError（错误码 504），后台任务继续运行至完成，
    其结果被丢弃（查询口径不同缓存键不同，不会产生脏缓存）。

后台线程为 daemon 线程，进程退出时不会被残余任务阻塞。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, TypeVar

from app.core.exceptions import ComputationTimeoutError

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_with_timeout(
    fn: Callable[[], T],
    timeout_seconds: float | None,
    task_name: str = "计算任务",
) -> T:
    """在限时内执行 fn；超时抛 ComputationTimeoutError，未超时透传结果/异常。

    timeout_seconds 为 None 或 <= 0 表示不限制，直接同步执行。
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        return fn()

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def _target() -> None:
        try:
            result_box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 —— 需把任意异常透传给调用线程
            error_box["error"] = exc

    thread = threading.Thread(target=_target, name=f"timeout-{task_name}", daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        logger.warning(
            "超时降级触发: %s 超过阈值 %.1fs（错误码 504），后台任务继续运行并丢弃结果",
            task_name, timeout_seconds,
        )
        raise ComputationTimeoutError(
            seconds=float(timeout_seconds),
            detail={"task": task_name, "timeout_seconds": float(timeout_seconds)},
        )

    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")  # type: ignore[return-value]
