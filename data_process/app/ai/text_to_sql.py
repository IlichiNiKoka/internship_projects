# -*- coding: utf-8 -*-
"""Text-to-SQL 模块已废弃（2026-08-24 架构调整）。

用户要求：
  1. 取消输入后本地意图识别，直接让 LLM 规划工具；
  2. 不再生成裸 SQL 去 MySQL 查，改为 LLM 输出 Spark Aggregation 的结构化参数
     （dimensions/metrics/filters/sort/limit），由 AggregationService 调用 Spark
     接口。这天然复用了：
       * Spark DataFrame 的数据加载缓存（避免重复 JDBC 扫表）
       * 注册表 registry.py 的维度/指标白名单安全校验
       * 慢查询告警与超时控制
       * AggregationService 自身的 Cache

本文件保留 `DBConfig` 与 `run_text_to_sql` 两个符号，以便历史代码中仍
`from app.ai.text_to_sql import ...` 的导入不会崩。但实际执行会立即
返回带「已废弃」标记的失败结果，不向数据库发送任何 SQL。

需要真正的查询能力请走 `app.ai.agent.ToolPlanningAgent` 或
`app.ai.service.AIService.execute`。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    db: str
    table: str


@dataclass
class SQLExecutionResult:
    generated_sql: str | None = None
    sql_explanation: str = ""
    success: bool = False
    error: str | None = None
    columns: list = None  # type: ignore[assignment]
    rows: list = None  # type: ignore[assignment]
    row_count: int = 0
    row_limit_applied: int = 0
    elapsed_seconds: float = 0.0

    def __post_init__(self):
        if self.columns is None:
            self.columns = []
        if self.rows is None:
            self.rows = []

    def to_analysis_result(self) -> dict[str, Any]:
        return {
            "error": "text_to_sql_deprecated",
            "error_detail": (
                self.error
                or "已废弃：请改用 Spark aggregation 接口（AIService.execute / Agent）"
            ),
            "generated_sql": self.generated_sql,
            "rows": [],
            "columns": [],
        }


_DEPRECATED_MSG = (
    "Text-to-SQL 模块已废弃。请改用 app.ai.agent.ToolPlanningAgent "
    "或 AIService.execute，LLM 输出 aggregation 参数，由 Spark 安全查数据。"
)


def build_system_prompt(*args: Any, **kwargs: Any) -> str:
    return _DEPRECATED_MSG


def build_user_prompt(*args: Any, **kwargs: Any) -> str:
    return _DEPRECATED_MSG


def run_text_to_sql(*, query: str, llm_client=None, **kwargs: Any) -> SQLExecutionResult:
    logger.warning("run_text_to_sql 被调用但已废弃: query=%r", query)
    return SQLExecutionResult(
        generated_sql=None,
        sql_explanation="",
        success=False,
        error=_DEPRECATED_MSG,
        columns=[],
        rows=[],
        row_count=0,
        row_limit_applied=0,
        elapsed_seconds=0.0,
    )
