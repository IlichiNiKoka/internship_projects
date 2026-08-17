"""医疗大数据清洗流水线（主脚本）。

功能总览
--------
对 SPARCS 2021 住院出院数据（约 832 MB / 2,101,588 行）执行分块清洗，
输出：
  1) 清洗后的 CSV（processed/*_clean.csv）
  2) 处理报告 JSON（processed/processing_report.json）

处理步骤及原因（详见各函数 docstring）：
  1. 定位原始 CSV：数据已解压在 raw/ 下（7z 解压时会额外套一层同名目录，
     故用递归搜索，而不是写死路径）。
  2. 分块读取：2.1M 行一次性载入会占用大量内存，按 CHUNK_SIZE 分块读取，
     只在内存中保留“当前块 + 去重用的哈希集合”，峰值内存可控。
  3. 逐字段清洗：列名统一、文本去空白+缺失填 Unknown、编码保前导零、
     整数/金额转数值、性别/急诊标志做映射（逻辑见 cleaners.py）。
  4. 跨分块去重：用“整行内容的哈希”做流式全局去重，避免同一住院记录被
     重复统计（比只在单块内 drop_duplicates 更彻底，见 _deduplicate）。
  5. 流式写出：边清洗边追加写 CSV，全程不把整张表 hold 在内存里。
  6. 生成报告：记录原始/清洗后行数、去重数量、各字段缺失量、关键字段分布，
     供人工核验与审计。
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from pathlib import Path

import pandas as pd

import cleaning_config as config
import cleaners


# ---------------------------------------------------------------------------
# 定位原始 CSV（含兜底解压）
# ---------------------------------------------------------------------------
def find_source_csv() -> Path:
    """在 raw/ 目录下递归查找原始 CSV；找不到则尝试从 RAR 解压。

    为什么：
      RAR 解压时若“解压到同名文件夹”，会产生 raw/<同名目录>/<同名>.csv 的
      嵌套结构，写死一层路径会很脆弱。递归搜索能兼容这种结构，也能兼容
      未来直接把 CSV 平铺到 raw/ 下的情况。
    """
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    candidates = [p for p in config.RAW_DIR.rglob("*.csv") if p.is_file()]
    if candidates:
        # 取体积最大的 CSV，避免误取到其他小文件
        return max(candidates, key=lambda p: p.stat().st_size)
    return _extract_archive()


def _extract_archive() -> Path:
    """从原始 RAR 解压并返回 CSV 路径（兜底路径，数据已解压时不会走到这里）。"""
    if not config.ARCHIVE_PATH.exists():
        raise FileNotFoundError(
            f"raw 目录下没有 CSV，且找不到归档文件：{config.ARCHIVE_PATH}"
        )
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [config.SEVEN_ZIP, "x", str(config.ARCHIVE_PATH), f"-o{config.RAW_DIR}", "-y"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    candidates = [p for p in config.RAW_DIR.rglob("*.csv") if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"解压后仍未找到 CSV：{config.RAW_DIR}")
    return max(candidates, key=lambda p: p.stat().st_size)


# ---------------------------------------------------------------------------
# 跨分块去重
# ---------------------------------------------------------------------------
def _deduplicate(df: pd.DataFrame, seen_hashes: set) -> tuple[pd.DataFrame, int]:
    """用整行哈希做流式全局去重，返回 (保留的行, 被去重的行数)。

    为什么不用单块内的 drop_duplicates()？
      单块内去重只能去掉“恰好落在同一块”里的重复行；相同记录若分散在相邻
      两个分块的边界上，会各自保留一次，去重不彻底。用“整行内容的哈希 +
      一个全局已见集合”就能跨块判断重复，且无需把整张表读进内存。

    为什么用整行哈希而不是某几个字段当主键？
      医疗数据没有明确的天然主键（同一患者可有多次住院），重复的定义
      就是“所有字段完全相同的两条记录”。对整行哈希即可精确匹配这个定义，
      也避免了人工挑主键可能带来的漏判。

    内存代价：
      需要维护一个最多约 210 万个 uint64 的集合（约百 MB 级），在本数据规模
      下可接受。若未来数据量大到内存放不下，可改为“按哈希分桶落盘再逐桶去重”
      或直接用 Spark/Dask 等分布式方案。
    """
    if df.empty:
        return df, 0
    hashes = [int(h) for h in pd.util.hash_pandas_object(df, index=False).tolist()]
    keep_mask = [h not in seen_hashes for h in hashes]
    kept = df[keep_mask]
    seen_hashes.update(hashes)          # 新出现的哈希加入“已见集合”
    removed = len(hashes) - int(sum(keep_mask))
    return kept, removed


# ---------------------------------------------------------------------------
# 处理报告
# ---------------------------------------------------------------------------
def build_report(
    source_csv: Path,
    source_size_bytes: int,
    raw_rows: int,
    clean_rows: int,
    duplicate_rows_removed: int,
    missing_counts: dict,
    distributions: dict,
    elapsed_seconds: float,
) -> dict:
    """组装处理报告（结构化 JSON，便于程序读取与人工审计）。"""
    return {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_csv": str(source_csv),
            "source_size_mb": round(source_size_bytes / (1024 * 1024), 2),
            "output_csv": str(config.CLEAN_CSV),
            "chunk_size": config.CHUNK_SIZE,
            "encoding": config.SOURCE_ENCODING,
            "elapsed_seconds": round(elapsed_seconds, 2),
        },
        "rows": {
            "raw_rows": raw_rows,
            "clean_rows": clean_rows,
            "duplicate_rows_removed": duplicate_rows_removed,
        },
        # 清洗后各字段的缺失量：文本字段统计 "Unknown" 个数，数值字段统计 NaN 个数
        "missing_or_unknown_by_column": missing_counts,
        # 关键字段的取值分布，用于核验清洗效果
        "key_field_distributions": distributions,
        # 清洗规则说明（“做了什么 + 为什么”的结构化摘要，全文见 README / data_dictionary）
        "processing_rules": [
            {
                "step": "column_normalization",
                "what": "统一列名为小写下划线（snake_case）",
                "why": "消除原始列名中的空格/连字符/括号差异，保证全链路字段名一致",
            },
            {
                "step": "string_trim_and_unknown",
                "what": "文本/分类字段去空白、折叠空白，缺失统一填充为 'Unknown'",
                "why": "避免 'Male '/'Male' 被当成两个值；统一缺失哨兵便于过滤与统计",
            },
            {
                "step": "code_preserve_leading_zero",
                "what": "编码/标识字段保留字符串原样（含前导零），缺失填 'Unknown'",
                "why": "机构号/DRG/MDC/邮编等是编码而非测量值，前导零有意义，不能转数值",
            },
            {
                "step": "numeric_conversion",
                "what": "整数/金额字段去逗号转数值，失败置 NaN",
                "why": "金额带千分位逗号且为字符串，不转数值无法求和/建模",
            },
            {
                "step": "gender_and_yn_mapping",
                "what": "性别 M/F/U -> Male/Female/Unknown；急诊标志只保留 Y/N",
                "why": "统一取值域，避免缩写/大小写混用导致统计分裂",
            },
            {
                "step": "global_deduplication",
                "what": "用整行哈希跨分块去重",
                "why": "去掉完全重复的住院记录，避免重复统计；跨块去重比单块内更彻底",
            },
        ],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run() -> dict:
    """执行完整清洗流程并返回报告 dict。"""
    start = time.time()

    source_csv = find_source_csv()
    source_size_bytes = source_csv.stat().st_size

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if config.CLEAN_CSV.exists():
        config.CLEAN_CSV.unlink()

    raw_rows = 0
    clean_rows = 0
    duplicate_rows_removed = 0

    # 各字段缺失量统计（文本 -> "Unknown" 计数，数值 -> NaN 计数）
    missing_counts: Counter = Counter()
    # 关键字段取值分布
    distributions: dict = {f: Counter() for f in config.REPORT_DISTRIBUTION_FIELDS}

    seen_hashes: set = set()
    wrote_header = False

    reader = pd.read_csv(
        source_csv,
        chunksize=config.CHUNK_SIZE,
        low_memory=False,
        dtype=str,          # 全部按字符串读入，再由我们显式转类型，见下方“为什么”
        encoding=config.SOURCE_ENCODING,
    )

    for chunk in reader:
        raw_rows += len(chunk)

        cleaned = cleaners.clean_chunk(chunk)
        cleaned, removed = _deduplicate(cleaned, seen_hashes)
        duplicate_rows_removed += removed

        # 统计缺失量与分布（基于清洗后、去重后的数据）
        for col in config.TEXT_COLUMNS + config.CODE_COLUMNS + config.PAYMENT_COLUMNS:
            if col in cleaned.columns:
                missing_counts[col] += int((cleaned[col] == config.UNKNOWN).sum())
        for col in config.INT_COLUMNS + config.MONEY_COLUMNS:
            if col in cleaned.columns:
                missing_counts[col] += int(cleaned[col].isna().sum())
        for f in config.REPORT_DISTRIBUTION_FIELDS:
            if f in cleaned.columns:
                distributions[f].update(cleaned[f].astype(str))

        clean_rows += len(cleaned)

        # 流式写出：首块带表头，后续追加
        cleaned.to_csv(
            config.CLEAN_CSV,
            mode="a",
            header=(not wrote_header),
            index=False,
            encoding="utf-8",
        )
        wrote_header = True

    # Counter 需要转成普通 dict 才能直接 json 序列化
    missing_plain = {k: int(v) for k, v in sorted(missing_counts.items())}
    distributions_plain = {
        k: dict(v.most_common(20)) for k, v in distributions.items()
    }

    report = build_report(
        source_csv=source_csv,
        source_size_bytes=source_size_bytes,
        raw_rows=raw_rows,
        clean_rows=clean_rows,
        duplicate_rows_removed=duplicate_rows_removed,
        missing_counts=missing_plain,
        distributions=distributions_plain,
        elapsed_seconds=time.time() - start,
    )

    config.REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    """命令行入口：运行并打印可读摘要。"""
    report = run()
    print("=" * 60)
    print("数据清洗完成")
    print("=" * 60)
    print(f"原始 CSV    : {report['meta']['source_csv']}")
    print(f"原始大小    : {report['meta']['source_size_mb']} MB")
    print(f"原始行数    : {report['rows']['raw_rows']:,}")
    print(f"清洗后行数  : {report['rows']['clean_rows']:,}")
    print(f"去重行数    : {report['rows']['duplicate_rows_removed']:,}")
    print(f"输出 CSV    : {report['meta']['output_csv']}")
    print(f"处理报告    : {config.REPORT_PATH}")
    print(f"耗时        : {report['meta']['elapsed_seconds']} 秒")


if __name__ == "__main__":
    main()
