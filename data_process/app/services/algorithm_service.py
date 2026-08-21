# -*- coding: utf-8 -*-
"""算法调度服务（需求 3.3.2）：统一算法调用入口。

二期 3.3.4：算法计算接入超时控制（超时 -> ComputationTimeoutError / 504）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.algorithms.base import AlgorithmContext, get_algorithm
from app.core.exceptions import ComputationError, ComputationTimeoutError
from app.core.timeout import run_with_timeout

logger = logging.getLogger(__name__)


class AlgorithmService:
    """把“统一算法接口”落地为服务编排：查注册表 -> 校验参数 -> 执行 -> 归一化结果。"""

    def __init__(self, provider, timeout_seconds: float | None = None):
        self._provider = provider
        self._timeout_seconds = timeout_seconds   # 二期：<=0 或 None 表示不限制

    def run(self, name: str, params: dict | None = None) -> dict:
        params = params or {}

        # 1) 注册中心查找（未注册 -> 404 备选流）
        algorithm = get_algorithm(name)

        # 2) 统一参数校验（类型/必填/取值范围 -> 失败 400 备选流）
        validated = algorithm.validate(params)
        logger.info("执行算法 %s，参数: %s", name, validated)

        # 3) 调度 Spark 任务执行（失败 -> 500 备选流；超时 -> 504 备选流）
        df = self._provider.dataframe()
        ctx = AlgorithmContext(dataframe=df, params=validated)
        try:
            result = run_with_timeout(
                lambda: algorithm.run(ctx),
                timeout_seconds=self._timeout_seconds,
                task_name=f"算法 {name}",
            )
        except ComputationTimeoutError:
            raise   # 超时降级（504）由中间件统一响应
        except ComputationError:
            raise

        # 4) 归一化结果（AlgorithmResult.to_dict）
        return result.to_dict()
