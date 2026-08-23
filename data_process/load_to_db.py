# -*- coding: utf-8 -*-
"""入库入口脚本：把清洗后 CSV 写入 MySQL（失败自动降级 SQLite）。

用法（在本目录 data_process/ 下执行）：
    python load_to_db.py                     # 用默认配置（auto：MySQL 优先）
    python load_to_db.py --engine sqlite     # 强制 SQLite
    python load_to_db.py --csv processed/xxx_clean.csv

参数说明：
    --engine  auto / mysql / sqlite（默认 auto：MySQL 连接失败自动降级 SQLite）
    --csv     清洗后 CSV 路径（默认取 processed/ 下 *_clean.csv）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 项目根加入 sys.path，保证 storage 包可导入（支持在任意目录执行）
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cleaning_config as config
from config.settings import Settings
from storage.loader import DatabaseLoader


def _default_csv() -> Path:
    """默认入库数据源：processed/ 下的 *_clean.csv。"""
    if config.CLEAN_CSV.exists():
        return config.CLEAN_CSV
    candidates = [p for p in config.OUTPUT_DIR.glob("*_clean.csv") if p.is_file()]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_size)
    raise FileNotFoundError(
        f"未找到清洗后 CSV：{config.OUTPUT_DIR}（请先运行 python run_pipeline.py）"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="清洗后数据入库 MySQL/SQLite")
    parser.add_argument("--engine", choices=["auto", "mysql", "sqlite"], default="auto")
    parser.add_argument("--csv", type=Path, default=None, help="清洗后 CSV 路径")
    args = parser.parse_args()

    csv_path = args.csv or _default_csv()
    settings = Settings.load()
    loader = DatabaseLoader.from_settings(csv_path, settings, engine=args.engine)
    report = loader.load()

    print("=" * 60)
    print("数据入库完成")
    print("=" * 60)
    print(f"源 CSV     : {report['meta']['source_csv']}")
    print(f"目标引擎   : {report['meta']['target_engine']}")
    print(f"目标地址   : {report['meta']['target']}")
    print(f"数据表     : {report['meta']['table']}")
    print(f"读取行数   : {report['rows']['read_rows']:,}")
    print(f"插入行数   : {report['rows']['inserted_rows']:,}")
    print(f"跳过重复   : {report['rows']['skipped_rows']:,}")
    print(f"耗时       : {report['meta']['elapsed_seconds']} 秒")
    if report["meta"]["fallback_reason"]:
        print(f"兜底原因   : {report['meta']['fallback_reason']}")
    print("=" * 60)

    # 报告落盘到 processed/ 下，供审计与质量核验
    report_path = config.OUTPUT_DIR / "db_load_report.json"
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"入库报告   : {report_path}")


if __name__ == "__main__":
    main()
