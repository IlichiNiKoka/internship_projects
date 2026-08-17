# -*- coding: utf-8 -*-
"""大数据算法封装（需求 3.3.2）—— 统一算法接口与注册中心。

设计：
  * 所有算法继承 Algorithm 基类，实现 run(context)；
  * 通过 @register_algorithm 装饰器注册进全局注册中心；
  * 参数规格 ParamSpec 声明式定义，统一完成 类型/必填/取值范围 校验；
  * 运行结果统一归一化为 AlgorithmResult（status/result/metrics/耗时）。

上层调用（多维度聚合分析、AI 智能交互模块、REST API）只需：
    algorithm = get_algorithm(name)
    algorithm.validate(params)
    result = algorithm.run(AlgorithmContext(df, params))
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pyspark.sql import DataFrame

from app.core.exceptions import ComputationError, ParamValidationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 参数规格与校验
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str = "any"            # str / int / float / bool / list / dict / any
    required: bool = False
    default: Any = None
    allowed_values: tuple | None = None   # 取值枚举（可选）
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""


def validate_params(specs: list[ParamSpec], params: dict) -> dict:
    """按规格校验参数，返回补齐默认值后的参数字典；非法时抛 ParamValidationError。"""
    errors: dict[str, str] = {}
    merged: dict = dict(params or {})

    for spec in specs:
        if spec.name not in merged or merged[spec.name] is None:
            if spec.required:
                errors[spec.name] = f"缺少必填参数 {spec.name}"
            elif spec.default is not None:
                merged[spec.name] = spec.default
            continue

        value = merged[spec.name]
        # 类型校验
        type_ok = _check_type(value, spec.type)
        if not type_ok:
            errors[spec.name] = f"参数类型应为 {spec.type}，实际为 {type(value).__name__}"
            continue
        # 枚举/范围校验
        if spec.allowed_values is not None and value not in spec.allowed_values:
            errors[spec.name] = f"取值必须为 {list(spec.allowed_values)} 之一"
        if isinstance(value, (int, float)):
            if spec.min_value is not None and value < spec.min_value:
                errors[spec.name] = f"取值不能小于 {spec.min_value}"
            if spec.max_value is not None and value > spec.max_value:
                errors[spec.name] = f"取值不能大于 {spec.max_value}"

    if errors:
        raise ParamValidationError(detail=errors, message=f"算法参数校验失败: {errors}")
    return merged


def _check_type(value: Any, expected: str) -> bool:
    if expected == "any":
        return True
    mapping = {
        "str": str, "int": int, "float": float, "bool": bool,
        "list": list, "dict": dict,
    }
    py_type = mapping[expected]
    # 注意：bool 是 int 子类，须先于 int 判断
    if py_type is bool:
        return isinstance(value, bool)
    return isinstance(value, py_type)


# ---------------------------------------------------------------------------
# 上下文与结果契约
# ---------------------------------------------------------------------------
@dataclass
class AlgorithmContext:
    """算法运行上下文：统一入口，屏蔽底层数据来源差异。"""
    dataframe: DataFrame
    params: dict = field(default_factory=dict)


@dataclass
class AlgorithmResult:
    """算法归一化结果契约（所有算法输出结构一致）。"""
    algorithm: str
    status: str = "success"          # success / failed
    result: Any = None               # 业务结果
    metrics: dict = field(default_factory=dict)   # 算法自身评估指标（RMSE 等）
    elapsed_seconds: float = 0.0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "status": self.status,
            "message": self.message,
            "elapsed_seconds": self.elapsed_seconds,
            "metrics": self.metrics,
            "result": self.result,
        }


# ---------------------------------------------------------------------------
# 算法基类与注册中心
# ---------------------------------------------------------------------------
class Algorithm(ABC):
    """算法组件基类。子类必须声明类属性并实现 run()。"""

    name: str = ""                    # 唯一键（API 路径使用，小写下划线）
    display_name: str = ""            # 中文名
    version: str = "1.0.0"
    description: str = ""
    tags: tuple = ()
    param_specs: list[ParamSpec] = []

    def validate(self, params: dict) -> dict:
        """参数校验 + 默认值补齐。"""
        return validate_params(self.param_specs, params)

    def run(self, ctx: AlgorithmContext) -> AlgorithmResult:
        """统一执行入口：计时 + 异常归一化。子类实现 _execute()。"""
        start = time.perf_counter()
        try:
            result, metrics, message = self._execute(ctx)
            return AlgorithmResult(
                algorithm=self.name,
                status="success",
                result=result,
                metrics=metrics or {},
                elapsed_seconds=round(time.perf_counter() - start, 3),
                message=message or "",
            )
        except ComputationError:
            raise
        except Exception as exc:  # 任务失败 -> 错误码 500（备选流 B）
            logger.exception("算法 %s 执行失败", self.name)
            raise ComputationError(
                message=f"算法任务执行失败: {self.name}",
                detail={"algorithm": self.name, "error": str(exc)},
            ) from exc

    @abstractmethod
    def _execute(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        """返回 (result, metrics, message)。"""

    def meta(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "tags": list(self.tags),
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "required": p.required,
                    "default": p.default,
                    "allowed_values": list(p.allowed_values) if p.allowed_values else None,
                    "min_value": p.min_value,
                    "max_value": p.max_value,
                    "description": p.description,
                }
                for p in self.param_specs
            ],
        }


_ALGORITHM_REGISTRY: dict[str, Algorithm] = {}


def register_algorithm(cls):
    """类装饰器：注册算法组件。"""
    _ALGORITHM_REGISTRY[cls.name] = cls()
    return cls


def get_algorithm(name: str) -> Algorithm:
    from app.core.exceptions import AlgorithmNotFoundError
    if name not in _ALGORITHM_REGISTRY:
        raise AlgorithmNotFoundError(name)
    return _ALGORITHM_REGISTRY[name]


def list_algorithms() -> list[dict]:
    return [alg.meta() for alg in _ALGORITHM_REGISTRY.values()]


def register_builtin_algorithms() -> None:
    """导入并注册全部内置算法（幂等）。"""
    from app.algorithms import association, cost_prediction, group_aggregation, readmission_risk, statistics  # noqa: F401
    logger.info("已注册算法组件: %s", ", ".join(sorted(_ALGORITHM_REGISTRY)))
