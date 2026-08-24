# -*- coding: utf-8 -*-
"""入库器（人员2 · 结构化大数据入库 · 二期 MySQL / SQLite 兜底）。

设计要点
--------
  1. **分块读取**：复用清洗流水线的分块思路，清洗后 CSV 约 210 万行，
     按 batch_size 分块读取，避免整表载入内存；
  2. **类型还原**：字符串列按 str 读入，数值列转回可空 Int64 / float，
     与清洗后数据字典保持一致，保证 `row_hash` 跨批次稳定；
  3. **行哈希唯一键**：用 `pd.util.hash_pandas_object(..., categorize=False)`
     对 33 个业务字段计算确定性哈希，写入 `row_hash` 唯一键列；
     `categorize=False` 保证同一行无论落在哪个分块、哪次运行都得到相同哈希
     （增量更新的判定基础，见需求 UC7）；
  4. **增量去重**：MySQL 用 `INSERT IGNORE`、SQLite 用 `INSERT OR IGNORE`，
     唯一键冲突即跳过，避免重复入库；
  5. **MySQL 优先 + SQLite 兜底**：MySQL 连接失败（未安装驱动 / 服务不可达 /
     认证失败等）时按 engine 策略降级 SQLite，并记录兜底原因到报告。
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

import pandas as pd

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

logger = logging.getLogger(__name__)

# 入库时读取清洗后 CSV 的批次行数（与清洗 CHUNK_SIZE 无关，可独立调优）
DEFAULT_BATCH_SIZE = 10_000


class DatabaseLoader:
    """把清洗后 CSV 批量写入 MySQL（失败降级 SQLite）。"""

    def __init__(
        self,
        csv_path: Path,
        *,
        engine: str = "auto",          # auto / mysql / sqlite
        batch_size: int = DEFAULT_BATCH_SIZE,
        table: str = "sparcs_discharge_2021",
        # ---- MySQL 连接参数 ----
        mysql_host: str = "127.0.0.1",
        mysql_port: int = 3306,
        mysql_user: str = "root",
        mysql_password: str = "",
        mysql_database: str = "medical_analytics",
        # ---- SQLite 兜底参数 ----
        sqlite_path: Path | None = None,
    ):
        self._csv_path = csv_path
        self._engine = engine
        self._batch_size = batch_size
        self._table = table
        self._mysql_host = mysql_host
        self._mysql_port = mysql_port
        self._mysql_user = mysql_user
        self._mysql_password = mysql_password
        self._mysql_database = mysql_database
        self._sqlite_path = sqlite_path

    # ------------------------------------------------------------------
    @classmethod
    def from_settings(cls, csv_path: Path, settings, *, engine: str | None = None) -> "DatabaseLoader":
        """从集中配置构造实例（config.settings.Settings 的 db_* 字段）。

        让 db_engine / db_host / db_user / db_batch_size 等由 .env 或环境变量
        （ANALYTICS_DB_*）统一控制，业务入口不再写死连接信息。
        """
        return cls(
            csv_path,
            engine=engine or getattr(settings, "db_engine", "auto"),
            batch_size=getattr(settings, "db_batch_size", DEFAULT_BATCH_SIZE),
            table=getattr(settings, "db_table", "sparcs_discharge_2021"),
            mysql_host=getattr(settings, "db_host", "127.0.0.1"),
            mysql_port=getattr(settings, "db_port", 3306),
            mysql_user=getattr(settings, "db_user", "root"),
            mysql_password=getattr(settings, "db_password", ""),
            mysql_database=getattr(settings, "db_name", "medical_analytics"),
            sqlite_path=getattr(settings, "db_sqlite_path", None),
        )

    # ------------------------------------------------------------------
    def load(self) -> dict:
        """执行入库并返回报告 dict。"""
        if not self._csv_path.exists():
            raise FileNotFoundError(
                f"清洗后 CSV 不存在: {self._csv_path}（请先运行数据清洗流水线）"
            )

        start = time.time()
        target_engine = "sqlite"
        target_desc = ""
        fallback_reason = ""
        conn = None

        # 1) 选择目标库：优先 MySQL，失败降级 SQLite
        if self._engine in {"auto", "mysql"}:
            try:
                conn, target_desc = self._open_mysql()
                target_engine = "mysql"
            except Exception as exc:  # MySQL 不可用
                if self._engine == "mysql":
                    raise RuntimeError(f"MySQL 连接失败（engine=mysql 不降级）: {exc}") from exc
                logger.warning("MySQL 不可用，降级 SQLite: %s", exc)
                fallback_reason = f"{type(exc).__name__}: {exc}"

        if conn is None:
            conn, target_desc = self._open_sqlite()

        # 2) 建表 + 索引
        self._ensure_schema(conn, target_engine)

        # 3) 分块读取 -> 类型还原 -> 行哈希 -> 批量写入
        read_rows = 0
        inserted_rows = 0
        skipped_rows = 0
        insert_sql = self._insert_sql(target_engine)

        reader = pd.read_csv(
            self._csv_path,
            chunksize=self._batch_size,
            dtype={col: str for col in STRING_FIELDS},
            low_memory=False,
        )
        for chunk in reader:
            normalized = self._normalize_types(chunk)
            read_rows += len(normalized)
            rows = self._to_rows(normalized)

            cursor = conn.cursor()
            cursor.executemany(insert_sql, rows)
            conn.commit()
            # rowcount = 本批实际插入的行数（INSERT IGNORE / OR IGNORE 被唯一键
            # 跳过的重复行不计入），据此精确统计增量去重数量
            inserted_rows += cursor.rowcount
            cursor.close()

        skipped_rows = read_rows - inserted_rows
        elapsed = round(time.time() - start, 2)
        conn.close()

        report = {
            "meta": {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source_csv": str(self._csv_path),
                "target_engine": target_engine,
                "target": target_desc,
                "table": self._table,
                "batch_size": self._batch_size,
                "elapsed_seconds": elapsed,
                "fallback_reason": fallback_reason or None,
            },
            "rows": {
                "read_rows": read_rows,
                "inserted_rows": inserted_rows,
                "skipped_rows": skipped_rows,
            },
            "indexes": list(INDEX_COLUMNS),
            "unique_key": ROW_HASH_COLUMN,
            # UC7 增量更新：本次入库的新增行 vs 被唯一键跳过的重复行
            "incremental": {
                "mode": "row_hash_dedup",
                "new_rows_inserted": inserted_rows,
                "duplicate_rows_skipped": skipped_rows,
            },
        }
        logger.info(
            "入库完成: %s -> %s（读取 %d 行，插入 %d 行，跳过重复 %d 行，耗时 %.2fs）",
            self._csv_path.name, target_desc, read_rows, inserted_rows,
            skipped_rows, elapsed,
        )
        return report

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def _open_mysql(self) -> tuple:
        """连接 MySQL，返回 (conn, 描述串)。连接前自动创建目标数据库。"""
        import pymysql  # 延迟导入：未安装时抛 ImportError，走 SQLite 兜底

        # 先连接服务器（不指定库），确保目标数据库存在
        server = pymysql.connect(
            host=self._mysql_host,
            port=self._mysql_port,
            user=self._mysql_user,
            password=self._mysql_password,
            charset="utf8mb4",
        )
        with server.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{self._mysql_database}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        server.close()

        conn = pymysql.connect(
            host=self._mysql_host,
            port=self._mysql_port,
            user=self._mysql_user,
            password=self._mysql_password,
            database=self._mysql_database,
            charset="utf8mb4",
        )
        desc = (
            f"mysql://{self._mysql_host}:{self._mysql_port}/"
            f"{self._mysql_database}/{self._table}"
        )
        return conn, desc

    def _open_sqlite(self) -> tuple:
        """连接 SQLite（标准库，无需额外依赖）。"""
        import sqlite3

        path = self._sqlite_path or (
            self._csv_path.parent / "sparcs.db"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL;")  # 大批量写入更快
        desc = f"sqlite:///{path}"
        return conn, desc

    # ------------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------------
    def _ensure_schema(self, conn, engine: str) -> None:
        """按方言建表 + 建索引（幂等）。

        SQLite 的 DDL 含多条语句（建表 + 若干建索引），需用 executescript
        一次执行；MySQL 的 DDL 是单条 CREATE TABLE（索引内联），execute 即可。
        """
        if engine == "mysql":
            cursor = conn.cursor()
            try:
                cursor.execute(mysql_ddl(self._table))
                conn.commit()
            finally:
                cursor.close()
        else:
            conn.executescript(sqlite_ddl(self._table))
            conn.commit()

    def _insert_sql(self, engine: str) -> str:
        """生成批量插入 SQL（唯一键冲突时忽略，实现增量去重）。"""
        cols = ORDERED_FIELDS + [ROW_HASH_COLUMN]
        col_list = ", ".join(cols)
        if engine == "mysql":
            placeholders = ", ".join(["%s"] * len(cols))
            return (
                f"INSERT IGNORE INTO `{self._table}` ({col_list}) "
                f"VALUES ({placeholders})"
            )
        placeholders = ", ".join(["?"] * len(cols))
        return (
            f"INSERT OR IGNORE INTO {self._table} ({col_list}) "
            f"VALUES ({placeholders})"
        )

    # ------------------------------------------------------------------
    # 类型还原 + 行哈希 + 行组装
    # ------------------------------------------------------------------
    def _normalize_types(self, df: pd.DataFrame) -> pd.DataFrame:
        """把 CSV 块还原为与清洗后一致的列顺序与 dtype。

        为什么需要这一步：
          to_csv 会把 NaN 写成空字符串，Int64 也会被展平成普通数字字符串。
          直接对读回的字符串做哈希会与“真正的清洗结果”不一致，导致同一行
          在不同批次被算出不同哈希。这里显式还原：
            * 列顺序统一为 ORDERED_FIELDS（规范顺序，不受 CSV 物理顺序影响）；
            * 整型列转可空 Int64，浮点列转 float，缺失保持 NaN。
        """
        df = df[[c for c in ORDERED_FIELDS if c in df.columns]].copy()
        # 补齐缺失列（防御：CSV 若缺列则补 NaN/Unknown，保证 33 列齐全）
        for col in ORDERED_FIELDS:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[ORDERED_FIELDS]

        # 字符串列：清洗后均为有效值或 "Unknown"，防御性把 NaN 统一为 "Unknown"
        for col in STRING_FIELDS:
            df[col] = df[col].where(df[col].notna(), "Unknown").astype(str)

        for col in INT_FIELDS:
            numeric = pd.to_numeric(df[col], errors="coerce")
            df[col] = numeric.round().astype("Int64")

        for col in DOUBLE_FIELDS:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    def _to_rows(self, df: pd.DataFrame) -> list:
        """把 DataFrame 转成批量行 tuple 列表（列顺序 = 表列顺序）。

        行哈希用 `categorize=False`：直接对字段值做哈希，保证同一行无论落在
        哪个分块、哪次运行都得到相同哈希（增量更新的判定基础）。
        """
        hashes = pd.util.hash_pandas_object(
            df[ORDERED_FIELDS], index=False, categorize=False
        )
        df[ROW_HASH_COLUMN] = [f"{int(h):020d}" for h in hashes]

        column_values = []
        for col in ORDERED_FIELDS + [ROW_HASH_COLUMN]:
            if col == ROW_HASH_COLUMN:
                column_values.append([str(v) for v in df[col]])
            elif col in STRING_FIELDS:
                column_values.append([str(v) for v in df[col]])
            elif col in INT_FIELDS:
                column_values.append(_nullable_int_list(df[col]))
            else:  # DOUBLE_FIELDS
                column_values.append(_nullable_float_list(df[col]))
        return list(zip(*column_values))


def _nullable_int_list(series: pd.Series) -> list:
    """可空整型列 -> Python int 或 None。"""
    return [None if pd.isna(v) else int(v) for v in series]


def _nullable_float_list(series: pd.Series) -> list:
    """浮点列 -> Python float 或 None（NaN/Inf 视为 None，避免脏值入库）。"""
    out = []
    for v in series:
        if pd.isna(v):
            out.append(None)
        else:
            f = float(v)
            out.append(None if math.isnan(f) or math.isinf(f) else f)
    return out
