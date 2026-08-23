# -*- coding: utf-8 -*-
"""数据持久化存储包（人员2：结构化大数据入库 · 二期 MySQL/SQLite）。

本包负责把清洗后 CSV（processed/*_clean.csv）批量写入关系型数据库：
  * MySQL 优先（生产），MySQL 不可用时自动降级 SQLite（开发/演示兜底）；
  * 表结构与索引见 schema.py（33 业务字段 + 行哈希唯一键 + 自增主键）；
  * 入库逻辑见 loader.py（分块读取 + 批量 INSERT + 增量去重 + 报告）。
"""

from storage.loader import DatabaseLoader
from storage.schema import (
    DOUBLE_FIELDS,
    INT_FIELDS,
    ORDERED_FIELDS,
    STRING_FIELDS,
    mysql_ddl,
    sqlite_ddl,
)

__all__ = [
    "DatabaseLoader",
    "ORDERED_FIELDS",
    "STRING_FIELDS",
    "INT_FIELDS",
    "DOUBLE_FIELDS",
    "mysql_ddl",
    "sqlite_ddl",
]
