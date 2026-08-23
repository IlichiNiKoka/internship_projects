# -*- coding: utf-8 -*-
"""数据质量评估入口（人员2 · 二期 UC5）。

用法（在本目录 data_process/ 下执行）：
    python quality_report.py                 # 评估 processed/*_clean.csv
    python quality_report.py --csv path.csv  # 指定 CSV

产出：
    processed/quality_report.json  机器可读评分明细
    processed/quality_report.html  ECharts 可视化报告
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cleaning_config as config
from storage.quality import DataQualityAssessor, render_html


def _default_csv() -> Path:
    if config.CLEAN_CSV.exists():
        return config.CLEAN_CSV
    candidates = [p for p in config.OUTPUT_DIR.glob("*_clean.csv") if p.is_file()]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_size)
    raise FileNotFoundError(
        f"未找到清洗后 CSV：{config.OUTPUT_DIR}（请先运行 python run_pipeline.py）"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="数据质量评估（四维度评分）")
    parser.add_argument("--csv", type=Path, default=None, help="清洗后 CSV 路径")
    parser.add_argument("--chunk-size", type=int, default=200_000)
    args = parser.parse_args()

    csv_path = args.csv or _default_csv()
    report = DataQualityAssessor(csv_path, chunk_size=args.chunk_size).assess()

    json_path = config.OUTPUT_DIR / "quality_report.json"
    html_path = config.OUTPUT_DIR / "quality_report.html"
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")

    s = report["scores"]
    print("=" * 60)
    print("数据质量评估完成")
    print("=" * 60)
    print(f"总行数    : {report['meta']['total_rows']:,}")
    print(f"完整性    : {s['completeness']} 分")
    print(f"准确性    : {s['accuracy']} 分")
    print(f"一致性    : {s['consistency']} 分")
    print(f"时效性    : {s['timeliness']} 分")
    print(f"综合评分  : {s['overall']} 分（等级 {s['grade']}）")
    print(f"JSON 报告 : {json_path}")
    print(f"HTML 报告 : {html_path}")


if __name__ == "__main__":
    main()
