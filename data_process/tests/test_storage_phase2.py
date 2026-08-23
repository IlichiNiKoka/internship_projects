# -*- coding: utf-8 -*-
"""人员2 二期功能测试：UC5 质量评估 / UC6 备份 / UC7 增量 + 集中配置。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from config.settings import Settings
from storage.backup import DatabaseBackupManager, _find_tool, _with_mysql_pwd
from storage.loader import DatabaseLoader
from storage.quality import DataQualityAssessor, render_html
from storage.schema import ORDERED_FIELDS

# 一行“合法”数据的取值（覆盖全部 33 字段，字符串字段均非 Unknown）
VALID_VALUES = {
    "hospital_service_area": "New York City",
    "hospital_county": "Bronx",
    "operating_certificate_number": "0101000",
    "permanent_facility_id": "001169",
    "facility_name": "Test Hospital",
    "age_group": "30 to 49",
    "zip_code_3_digits": "104",
    "gender": "Male",
    "race": "White",
    "ethnicity": "Not Span/Hispanic",
    "type_of_admission": "Emergency",
    "patient_disposition": "Home or Self Care",
    "ccsr_diagnosis_code": "INF012",
    "ccsr_diagnosis_description": "Test Diagnosis",
    "ccsr_procedure_code": "OTR004",
    "ccsr_procedure_description": "Test Procedure",
    "apr_drg_code": "137",
    "apr_drg_description": "Test DRG",
    "apr_mdc_code": "05",
    "apr_mdc_description": "Test MDC",
    "apr_severity_of_illness_description": "Moderate",
    "apr_risk_of_mortality": "Moderate",
    "apr_medical_surgical_description": "Medical",
    "payment_typology_1": "Medicare",
    "payment_typology_2": "Unknown",
    "payment_typology_3": "Unknown",
    "emergency_department_indicator": "Y",
    "length_of_stay": "3",
    "discharge_year": "2021",
    "apr_severity_of_illness_code": "2",
    "birth_weight": "",  # 非新生儿 -> 缺失属正常
    "total_charges": "100.50",
    "total_costs": "50.25",
}


def _write_quality_csv(
    path: Path,
    n: int,
    *,
    bad_gender: bool = False,
    negative_charge: bool = False,
    bad_year: bool = False,
    with_dup: bool = False,
) -> None:
    rows = []
    for i in range(n):
        row = dict(VALID_VALUES)
        row["total_charges"] = str(100.0 + i)   # 保证行间唯一
        row["total_costs"] = str(50.0 + i)
        if bad_gender and i == 0:
            row["gender"] = "Malee"             # 非法取值
        if negative_charge and i == 1:
            row["total_charges"] = "-5"          # 负数
        if bad_year and i == 2:
            row["discharge_year"] = "1999"       # 异常年份
        rows.append(row)
    if with_dup:
        rows.append(dict(rows[0]))               # 追加一条完全重复
    pd.DataFrame(rows, columns=ORDERED_FIELDS).to_csv(path, index=False)


# ---------------------------------------------------------------------------
# 集中配置（from_settings）
# ---------------------------------------------------------------------------
def test_from_settings_reads_db_fields(tmp_path: Path):
    settings = Settings(
        db_engine="sqlite",
        db_host="10.0.0.1",
        db_port=3307,
        db_user="u",
        db_password="p",
        db_name="mydb",
        db_table="t9",
        db_batch_size=123,
        db_sqlite_path=tmp_path / "s.db",
    )
    loader = DatabaseLoader.from_settings(tmp_path / "x.csv", settings)
    assert loader._engine == "sqlite"
    assert loader._mysql_host == "10.0.0.1"
    assert loader._mysql_port == 3307
    assert loader._mysql_user == "u"
    assert loader._mysql_password == "p"
    assert loader._mysql_database == "mydb"
    assert loader._table == "t9"
    assert loader._batch_size == 123
    assert loader._sqlite_path == tmp_path / "s.db"

    # engine 参数可覆盖 settings
    forced = DatabaseLoader.from_settings(tmp_path / "x.csv", settings, engine="mysql")
    assert forced._engine == "mysql"


# ---------------------------------------------------------------------------
# UC5 数据质量评估
# ---------------------------------------------------------------------------
def test_quality_clean_data_full_score(tmp_path: Path):
    csv = tmp_path / "clean.csv"
    _write_quality_csv(csv, 50)
    report = DataQualityAssessor(csv, chunk_size=10).assess()

    s = report["scores"]
    assert s["completeness"] == 100.0
    assert s["accuracy"] == 100.0
    assert s["consistency"] == 100.0
    assert s["timeliness"] == 100.0
    assert s["overall"] == 100.0
    assert s["grade"] == "A"
    assert report["meta"]["total_rows"] == 50
    assert report["details"]["completeness"]["core_fields_missing"]


def test_quality_detects_dirty_values(tmp_path: Path):
    csv = tmp_path / "dirty.csv"
    _write_quality_csv(
        csv, 10, bad_gender=True, negative_charge=True, bad_year=True, with_dup=True
    )
    report = DataQualityAssessor(csv, chunk_size=10).assess()

    s = report["scores"]
    # 完整性不受“非法但非空”取值影响，仍为 100
    assert s["completeness"] == 100.0
    # 非法性别 -> 准确性下降
    assert s["accuracy"] < 100.0
    # 负数金额 + 重复行 -> 一致性下降
    assert s["consistency"] < 100.0
    # 出现 1999 -> 时效性下降
    assert s["timeliness"] < 100.0
    assert 0 <= s["overall"] <= 100
    assert s["grade"] in {"A", "B", "C", "D", "F"}

    # 时效性明细应显示异常年份（年份为整数键）
    years = report["details"]["timeliness"]["year_distribution"]
    assert years.get(1999) == 1
    assert years.get(2021) == 10


def test_render_html_contains_echarts(tmp_path: Path):
    csv = tmp_path / "clean.csv"
    _write_quality_csv(csv, 5)
    report = DataQualityAssessor(csv, chunk_size=5).assess()
    html = render_html(report)
    assert isinstance(html, str)
    assert "echarts" in html
    assert "数据质量" in html


# ---------------------------------------------------------------------------
# UC6 备份与恢复（工具定位 / 环境变量，不依赖真实 mysqldump）
# ---------------------------------------------------------------------------
def test_find_tool_locates_dummy(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mysqldump.exe").write_text("dummy", encoding="utf-8")
    assert _find_tool("mysqldump", str(bin_dir)) == str(bin_dir / "mysqldump.exe")
    assert _find_tool("mysql", str(bin_dir)) is None
    assert _find_tool("no_such_tool_xyz") is None


def test_with_mysql_pwd_handles_empty_and_secret():
    assert "MYSQL_PWD" not in _with_mysql_pwd("")
    assert _with_mysql_pwd("s3cret")["MYSQL_PWD"] == "s3cret"


def test_backup_manager_constructs(tmp_path: Path):
    mgr = DatabaseBackupManager(backup_dir=tmp_path / "backups")
    assert mgr._database == "medical_analytics"
    assert mgr._backup_dir == tmp_path / "backups"
