# -*- coding: utf-8 -*-
"""人员2 入库模块测试（SQLite 路径，无需真实 MySQL）。

覆盖：
  1. MySQL / SQLite 建表 DDL 生成；
  2. 基本入库：行数、目标引擎、唯一键；
  3. 重复入库（增量更新 UC7）：第二次全部被唯一键跳过；
  4. CSV 内含重复行：入库时按 row_hash 去重。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from storage.loader import DatabaseLoader
from storage.schema import (
    DOUBLE_FIELDS,
    INDEX_COLUMNS,
    INT_FIELDS,
    ORDERED_FIELDS,
    ROW_HASH_COLUMN,
    STRING_FIELDS,
    mysql_ddl,
    sqlite_ddl,
)


def _row(i: int) -> list:
    """按确定性规则生成第 i 行 33 字段（部分数值缺失）。"""
    row = []
    for col in STRING_FIELDS:
        row.append(f"{col}_{i % 7}")
    for col in INT_FIELDS:
        row.append("" if i % 9 == 0 else str(i % 20 + 1))
    for col in DOUBLE_FIELDS:
        row.append("" if i % 11 == 0 else str(round(100.0 + i * 1.5, 2)))
    return row


def _write_csv(path: Path, n: int) -> None:
    df = pd.DataFrame([_row(i) for i in range(n)], columns=ORDERED_FIELDS)
    df.to_csv(path, index=False)


def _write_csv_with_dup(path: Path, n_unique: int) -> None:
    """前 n_unique 行唯一，后 n_unique 行与前 n_unique 行完全相同。"""
    first = [_row(i) for i in range(n_unique)]
    df = pd.DataFrame(first + first, columns=ORDERED_FIELDS)
    df.to_csv(path, index=False)


def _count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DDL 生成
# ---------------------------------------------------------------------------
def test_mysql_ddl_contains_expected_parts():
    ddl = mysql_ddl("sparcs_discharge_2021")
    assert "CREATE TABLE IF NOT EXISTS" in ddl
    assert "UNIQUE KEY `uk_row_hash`" in ddl
    assert "ENGINE=InnoDB" in ddl
    assert "total_charges" in ddl
    for col in INDEX_COLUMNS:
        assert f"idx_{col}" in ddl


def test_sqlite_ddl_contains_expected_parts():
    ddl = sqlite_ddl("sparcs_discharge_2021")
    assert "CREATE TABLE IF NOT EXISTS" in ddl
    assert f"{ROW_HASH_COLUMN} TEXT NOT NULL UNIQUE" in ddl
    assert "total_costs" in ddl


# ---------------------------------------------------------------------------
# 基本入库
# ---------------------------------------------------------------------------
def test_load_to_sqlite_basic(tmp_path: Path):
    csv = tmp_path / "sample_clean.csv"
    _write_csv(csv, 100)

    loader = DatabaseLoader(csv, engine="sqlite", table="t1",
                            sqlite_path=tmp_path / "sparcs.db")
    report = loader.load()

    assert report["meta"]["target_engine"] == "sqlite"
    assert report["rows"]["read_rows"] == 100
    assert report["rows"]["inserted_rows"] == 100
    assert report["rows"]["skipped_rows"] == 0
    assert report["unique_key"] == ROW_HASH_COLUMN

    # 表存在且行数正确
    assert _count_rows(tmp_path / "sparcs.db", "t1") == 100


# ---------------------------------------------------------------------------
# 增量更新：重复入库被唯一键全部跳过
# ---------------------------------------------------------------------------
def test_load_dedup_on_rerun(tmp_path: Path):
    csv = tmp_path / "sample_clean.csv"
    _write_csv(csv, 60)
    db_path = tmp_path / "sparcs.db"

    first = DatabaseLoader(csv, engine="sqlite", table="t1",
                           sqlite_path=db_path).load()
    second = DatabaseLoader(csv, engine="sqlite", table="t1",
                            sqlite_path=db_path).load()

    assert first["rows"]["inserted_rows"] == 60
    assert second["rows"]["inserted_rows"] == 0
    assert second["rows"]["skipped_rows"] == 60
    assert _count_rows(db_path, "t1") == 60


# ---------------------------------------------------------------------------
# CSV 内含重复行：按 row_hash 去重
# ---------------------------------------------------------------------------
def test_load_dedup_within_csv(tmp_path: Path):
    csv = tmp_path / "dup_clean.csv"
    _write_csv_with_dup(csv, n_unique=50)  # 50 唯一 + 50 重复 = 100 行

    report = DatabaseLoader(csv, engine="sqlite", table="t1",
                            sqlite_path=tmp_path / "sparcs.db").load()

    assert report["rows"]["read_rows"] == 100
    assert report["rows"]["inserted_rows"] == 50
    assert report["rows"]["skipped_rows"] == 50
