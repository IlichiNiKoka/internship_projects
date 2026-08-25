# -*- coding: utf-8 -*-
"""一次性工具：MySQL -> Parquet 快照导出（消除 Spark JDBC 冷启动 ~30s 加载）。

用法（务必使用项目根目录 .venv 的 Python）：
    cd data_process
    ..\\.venv\\Scripts\\python.exe scripts/export_mysql_to_parquet.py

行为：
  1. 按 .env 配置通过 Spark JDBC 分区读取 MySQL 入库表；
  2. 对齐 SPARCS_SCHEMA（33 个业务字段）；
  3. 以 Snappy Parquet 覆盖写入 ANALYTICS_DATA_PARQUET_PATH
     （默认 processed/sparcs_snapshot.parquet）。
之后 MySQLDataProvider 启动时会直读该快照（见 app/data/data_provider.py），
不再走 JDBC 全表拉取。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings                      # noqa: E402
from app.utils.spark import build_spark_session           # noqa: E402
from app.data.data_provider import _cast_to_schema        # noqa: E402


def main() -> int:
    settings = Settings.load()
    snap = Path(settings.data_parquet_path)
    print(f"[1/3] 构建 SparkSession (master={settings.spark_master}) ...")
    spark = build_spark_session(settings)

    url = f"jdbc:mysql://{settings.db_host}:{settings.db_port}/{settings.db_name}"
    print(f"[2/3] JDBC 读取 MySQL: {url} table={settings.db_table} ...")
    t0 = time.perf_counter()
    max_id = (
        spark.read.format("jdbc")
        .option("url", url)
        .option("dbtable", f"(SELECT MAX(id) AS m FROM {settings.db_table}) t")
        .option("user", settings.db_user)
        .option("password", settings.db_password)
        .option("driver", settings.mysql_jdbc_driver)
        .load()
        .collect()[0][0]
    )
    df = (
        spark.read.format("jdbc")
        .option("url", url)
        .option("dbtable", settings.db_table)
        .option("user", settings.db_user)
        .option("password", settings.db_password)
        .option("driver", settings.mysql_jdbc_driver)
        .option("partitionColumn", "id")
        .option("lowerBound", "1")
        .option("upperBound", str(int(max_id) + 1))
        .option("numPartitions", "8")
        .load()
    )
    df = _cast_to_schema(df)

    print(f"[3/3] 写入 Parquet 快照: {snap} ...")
    snap.parent.mkdir(parents=True, exist_ok=True)
    df.write.mode("overwrite").option("compression", "snappy").parquet(snap.as_posix())
    count = spark.read.parquet(snap.as_posix()).count()
    print(f"完成：{count} 行，耗时 {time.perf_counter() - t0:.1f}s -> {snap}")
    spark.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
