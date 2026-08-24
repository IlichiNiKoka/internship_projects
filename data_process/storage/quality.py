# -*- coding: utf-8 -*-
"""数据质量评估（人员2 · 二期 UC5）。

对清洗后 CSV 从四个维度做**流式**评分（分块读取，避免整表载入内存）：
  * 完整性 Completeness —— 核心字段非空 / 非 Unknown 的比例；
  * 准确性 Accuracy     —— 取值是否落在合法值域内（编码/分类字段 + 数值范围）；
  * 一致性 Consistency  —— 跨字段业务规则（数值非负、新生儿应有出生体重、行级重复）；
  * 时效性 Timeliness   —— 数据所属报告期是否完整（出院年份覆盖）。

输出（由 quality_report.py 入口写出）：
  * processed/quality_report.json  —— 机器可读的评分明细；
  * processed/quality_report.html  —— 自包含 ECharts 可视化报告（雷达图 + 柱状图）。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from storage.schema import DOUBLE_FIELDS, INT_FIELDS, ORDERED_FIELDS, STRING_FIELDS

UNKNOWN = "Unknown"

# ---------------------------------------------------------------------------
# 字段分类（完整性维度）
# ---------------------------------------------------------------------------
# 核心字符串字段：这些字段理论上应完整，稀疏即异常
CORE_STRING_FIELDS = [
    "hospital_service_area", "hospital_county", "operating_certificate_number",
    "permanent_facility_id", "facility_name", "age_group", "zip_code_3_digits",
    "gender", "race", "ethnicity", "type_of_admission", "patient_disposition",
    "ccsr_diagnosis_code", "ccsr_diagnosis_description",
    "apr_drg_code", "apr_drg_description", "apr_mdc_code", "apr_mdc_description",
    "apr_severity_of_illness_description", "apr_risk_of_mortality",
    "apr_medical_surgical_description", "payment_typology_1",
    "emergency_department_indicator",
]

# 可空字符串字段：缺失属正常业务（次/三支付方式、手术操作）
OPTIONAL_STRING_FIELDS = [
    "payment_typology_2", "payment_typology_3",
    "ccsr_procedure_code", "ccsr_procedure_description",
]

CORE_INT_FIELDS = ["length_of_stay", "discharge_year", "apr_severity_of_illness_code"]
CORE_DOUBLE_FIELDS = ["total_charges", "total_costs"]
OPTIONAL_INT_FIELDS = ["birth_weight"]

# 完整性评分只看核心字段（可空字段不扣分，仅作为说明）
COMPLETENESS_FIELDS = CORE_STRING_FIELDS + CORE_INT_FIELDS + CORE_DOUBLE_FIELDS

# ---------------------------------------------------------------------------
# 值域（准确性维度）：不含 Unknown——Unknown 属于缺失而非“非法取值”
# ---------------------------------------------------------------------------
DOMAINS: dict[str, set[str]] = {
    "gender": {"Male", "Female"},
    "emergency_department_indicator": {"Y", "N"},
    "age_group": {"0 to 17", "18 to 29", "30 to 49", "50 to 69", "70 or Older"},
    "type_of_admission": {"Emergency", "Elective", "Newborn", "Urgent", "Trauma", "Not Available"},
    "apr_risk_of_mortality": {"Minor", "Moderate", "Major", "Extreme"},
    "apr_severity_of_illness_description": {"Minor", "Moderate", "Major", "Extreme"},
    "apr_medical_surgical_description": {"Medical", "Surgical", "Not Applicable"},
}

# 数值范围（准确性维度）：(下界, 上界)，闭区间
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "length_of_stay": (0, 3650),
    "discharge_year": (2000, 2100),
    "apr_severity_of_illness_code": (0, 4),
    "birth_weight": (0, 10000),
    "total_charges": (0.0, float("inf")),
    "total_costs": (0.0, float("inf")),
}

# ---------------------------------------------------------------------------
# 评分权重（四维度等权，可按业务调整）
# ---------------------------------------------------------------------------
WEIGHTS = {
    "completeness": 0.25,
    "accuracy": 0.25,
    "consistency": 0.25,
    "timeliness": 0.25,
}


def _grade(score: float) -> str:
    if score >= 95:
        return "A"
    if score >= 90:
        return "B"
    if score >= 80:
        return "C"
    if score >= 70:
        return "D"
    return "F"


def _round(v: float, nd: int = 2) -> float:
    return round(float(v), nd)


class DataQualityAssessor:
    """流式评估清洗后 CSV 的四维数据质量并产出报告 dict。"""

    def __init__(self, csv_path: Path, *, chunk_size: int = 200_000) -> None:
        self._csv_path = Path(csv_path)
        self._chunk_size = chunk_size

    # ------------------------------------------------------------------
    def assess(self) -> dict:
        if not self._csv_path.exists():
            raise FileNotFoundError(f"清洗后 CSV 不存在: {self._csv_path}")

        start = time.time()
        reader = pd.read_csv(
            self._csv_path, chunksize=self._chunk_size, dtype=str, low_memory=False
        )

        total_rows = 0
        # 完整性：核心字段缺失计数
        comp_missing: Counter = Counter()
        # 准确性：字段 -> (informative 数, 非法数)
        acc_info: Counter = Counter()
        acc_invalid: Counter = Counter()
        # 一致性
        nn_total = 0            # 数值非负检查基数
        nn_violations = 0       # 数值非负违规数
        nb_total = 0            # 新生儿行数
        nb_violations = 0       # 新生儿缺出生体重行数
        non_newborn_with_bw = 0  # 非新生儿但有出生体重（信息性提示）
        seen_hashes: set = set()
        duplicate_rows = 0
        # 时效性
        year_counter: Counter = Counter()

        for chunk in reader:
            total_rows += len(chunk)

            # ---- 完整性 ----
            for col in COMPLETENESS_FIELDS:
                if col not in chunk.columns:
                    continue
                series = chunk[col]
                if col in STRING_FIELDS:
                    missing = int((series.isna() | (series == UNKNOWN)).sum())
                else:
                    missing = int(pd.to_numeric(series, errors="coerce").isna().sum())
                comp_missing[col] += missing

            # ---- 准确性：值域 ----
            for col, domain in DOMAINS.items():
                if col not in chunk.columns:
                    continue
                series = chunk[col].astype(str)
                informative = series.notna() & (series != UNKNOWN)
                acc_info[col] += int(informative.sum())
                acc_invalid[col] += int((informative & ~series.isin(domain)).sum())

            # ---- 准确性：数值范围 ----
            for col, (lo, hi) in NUMERIC_RANGES.items():
                if col not in chunk.columns:
                    continue
                series = pd.to_numeric(chunk[col], errors="coerce")
                informative = series.notna()
                acc_info[col] += int(informative.sum())
                acc_invalid[col] += int((informative & ((series < lo) | (series > hi))).sum())

            # ---- 一致性 1：数值非负 ----
            for col in ["length_of_stay", "total_charges", "total_costs", "birth_weight"]:
                if col not in chunk.columns:
                    continue
                series = pd.to_numeric(chunk[col], errors="coerce")
                informative = series.notna()
                nn_total += int(informative.sum())
                nn_violations += int((informative & (series < 0)).sum())

            # ---- 一致性 2：新生儿应有出生体重 ----
            if "type_of_admission" in chunk.columns and "birth_weight" in chunk.columns:
                adm = chunk["type_of_admission"].astype(str)
                bw = pd.to_numeric(chunk["birth_weight"], errors="coerce")
                is_newborn = adm == "Newborn"
                nb_total += int(is_newborn.sum())
                nb_violations += int((is_newborn & bw.isna()).sum())
                non_newborn_with_bw += int((~is_newborn & bw.notna()).sum())

            # ---- 一致性 3：行级重复（按入库同款类型归一化后哈希）----
            hash_df = chunk[ORDERED_FIELDS].copy()
            for col in STRING_FIELDS:
                hash_df[col] = hash_df[col].where(hash_df[col].notna(), UNKNOWN).astype(str)
            for col in INT_FIELDS:
                hash_df[col] = pd.to_numeric(hash_df[col], errors="coerce").round().astype("Int64")
            for col in DOUBLE_FIELDS:
                hash_df[col] = pd.to_numeric(hash_df[col], errors="coerce")
            hashes = pd.util.hash_pandas_object(hash_df, index=False, categorize=False)
            for h in hashes:
                h = int(h)
                if h in seen_hashes:
                    duplicate_rows += 1
                else:
                    seen_hashes.add(h)

            # ---- 时效性：出院年份 ----
            if "discharge_year" in chunk.columns:
                years = pd.to_numeric(chunk["discharge_year"], errors="coerce").dropna()
                year_counter.update(int(y) for y in years)

        # ---- 汇总评分 ----
        completeness_score = self._completeness_score(comp_missing, total_rows)
        accuracy_score = self._ratio_score(acc_invalid, acc_info)
        consistency_score = self._consistency_score(
            nn_violations, nn_total, nb_violations, nb_total, duplicate_rows, total_rows
        )
        expected_year = year_counter.most_common(1)[0][0] if year_counter else None
        timeliness_score = self._timeliness_score(year_counter, total_rows)

        overall = _round(
            completeness_score * WEIGHTS["completeness"]
            + accuracy_score * WEIGHTS["accuracy"]
            + consistency_score * WEIGHTS["consistency"]
            + timeliness_score * WEIGHTS["timeliness"]
        )

        report = {
            "meta": {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source_csv": str(self._csv_path),
                "total_rows": total_rows,
                "chunk_size": self._chunk_size,
                "elapsed_seconds": _round(time.time() - start),
            },
            "scores": {
                "completeness": completeness_score,
                "accuracy": accuracy_score,
                "consistency": consistency_score,
                "timeliness": timeliness_score,
                "overall": overall,
                "grade": _grade(overall),
            },
            "details": {
                "completeness": {
                    "weight": WEIGHTS["completeness"],
                    "core_fields_missing": {
                        col: comp_missing[col] for col in COMPLETENESS_FIELDS
                    },
                    "optional_fields": list(OPTIONAL_STRING_FIELDS + OPTIONAL_INT_FIELDS),
                },
                "accuracy": {
                    "weight": WEIGHTS["accuracy"],
                    "checks": [
                        {
                            "field": col,
                            "informative": acc_info[col],
                            "invalid": acc_invalid[col],
                        }
                        for col in acc_info
                    ],
                },
                "consistency": {
                    "weight": WEIGHTS["consistency"],
                    "checks": [
                        {"rule": "non_negative", "total": nn_total, "violations": nn_violations},
                        {"rule": "newborn_birth_weight", "total": nb_total, "violations": nb_violations,
                         "info_non_newborn_with_birth_weight": non_newborn_with_bw},
                        {"rule": "duplicate_rows", "total": total_rows, "violations": duplicate_rows},
                    ],
                },
                "timeliness": {
                    "weight": WEIGHTS["timeliness"],
                    "expected_year": expected_year,
                    "year_distribution": dict(sorted(year_counter.items())),
                },
            },
        }
        return report

    # ------------------------------------------------------------------
    # 评分算法（子函数，便于单测）
    # ------------------------------------------------------------------
    @staticmethod
    def _completeness_score(missing: Counter, total_rows: int) -> float:
        if total_rows == 0 or not COMPLETENESS_FIELDS:
            return 0.0
        ratios = [1 - missing[col] / total_rows for col in COMPLETENESS_FIELDS]
        return _round(sum(ratios) / len(ratios) * 100)

    @staticmethod
    def _ratio_score(invalid: Counter, info: Counter) -> float:
        total_info = sum(info.values())
        total_invalid = sum(invalid.values())
        if total_info == 0:
            return 100.0
        return _round((1 - total_invalid / total_info) * 100)

    @staticmethod
    def _consistency_score(
        nn_violations: int, nn_total: int,
        nb_violations: int, nb_total: int,
        duplicate_rows: int, total_rows: int,
    ) -> float:
        sub_scores: list[float] = []
        if nn_total > 0:
            sub_scores.append(1 - nn_violations / nn_total)
        if nb_total > 0:
            sub_scores.append(1 - nb_violations / nb_total)
        if total_rows > 0:
            sub_scores.append(1 - duplicate_rows / total_rows)
        if not sub_scores:
            return 100.0
        return _round(sum(sub_scores) / len(sub_scores) * 100)

    @staticmethod
    def _timeliness_score(year_counter: Counter, total_rows: int) -> float:
        if not year_counter or total_rows == 0:
            return 100.0
        expected = year_counter.most_common(1)[0][0]
        return _round(year_counter[expected] / total_rows * 100)


# ---------------------------------------------------------------------------
# HTML 可视化报告（自包含，ECharts 走 CDN）
# ---------------------------------------------------------------------------
def render_html(report: dict) -> str:
    """把评估报告渲染为自包含 HTML（雷达图 + 核心字段完整度柱状图）。"""
    scores = report["scores"]
    total = report["meta"]["total_rows"]
    missing = report["details"]["completeness"]["core_fields_missing"]
    field_names = list(missing.keys())
    completeness_values = [
        _round((1 - missing[c] / total) * 100) if total else 0.0 for c in field_names
    ]

    data = {
        "dimensions": ["完整性", "准确性", "一致性", "时效性"],
        "radar": [
            scores["completeness"], scores["accuracy"],
            scores["consistency"], scores["timeliness"],
        ],
        "overall": scores["overall"],
        "grade": scores["grade"],
        "total_rows": total,
        "field_names": field_names,
        "completeness_values": completeness_values,
    }
    data_json = json.dumps(data, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>数据质量评估报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; margin: 0; background:#f5f7fa; }}
  .header {{ background:#1f2d3d; color:#fff; padding:24px 32px; }}
  .header h1 {{ margin:0; font-size:22px; }}
  .header .score {{ font-size:40px; font-weight:700; margin-top:8px; }}
  .header .grade {{ display:inline-block; margin-left:12px; padding:4px 14px; border-radius:6px;
                     background:#67c23a; color:#fff; font-size:20px; vertical-align:middle; }}
  .grid {{ display:flex; flex-wrap:wrap; gap:16px; padding:24px 32px; }}
  .card {{ background:#fff; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,.06); padding:16px; }}
  #radar {{ width:480px; height:360px; }}
  #bars {{ width:640px; height:440px; }}
  .meta {{ color:#606266; font-size:13px; padding:0 32px 24px; }}
</style>
</head>
<body>
<div class="header">
  <h1>数据质量评估报告 · 四维评分</h1>
  <div><span class="score">{scores['overall']}</span><span class="grade">{scores['grade']}</span></div>
</div>
<div class="grid">
  <div class="card" id="radar"></div>
  <div class="card" id="bars"></div>
</div>
<div class="meta">
  数据行数：{total:,}　|　完整性 {scores['completeness']} · 准确性 {scores['accuracy']} · 一致性 {scores['consistency']} · 时效性 {scores['timeliness']}
</div>
<script>
const DATA = {data_json};
const radar = echarts.init(document.getElementById('radar'));
radar.setOption({{
  title: {{ text: '四维评分雷达图', left: 'center' }},
  tooltip: {{}},
  radar: {{
    indicator: DATA.dimensions.map(d => ({{ name: d, max: 100 }})),
    radius: '65%'
  }},
  series: [{{
    type: 'radar',
    data: [{{ value: DATA.radar, name: '质量评分' }}],
    areaStyle: {{ opacity: 0.25 }}
  }}]
}});
const bars = echarts.init(document.getElementById('bars'));
bars.setOption({{
  title: {{ text: '核心字段完整度（%）', left: 'center' }},
  tooltip: {{ trigger: 'axis' }},
  grid: {{ left: '3%', right: '4%', bottom: '20%', containLabel: true }},
  xAxis: {{ type: 'category', data: DATA.field_names, axisLabel: {{ rotate: 60, interval: 0, fontSize: 10 }} }},
  yAxis: {{ type: 'value', min: 0, max: 100 }},
  series: [{{ type: 'bar', data: DATA.completeness_values, barMaxWidth: 22,
               itemStyle: {{ color: '#409eff' }} }}]
}});
</script>
</body>
</html>
"""
