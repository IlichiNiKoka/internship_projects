# -*- coding: utf-8 -*-
"""pytest 全局 fixture：测试用 SparkSession、样本数据、应用与客户端。

测试数据不使用真实 210 万行 CSV，而是内存构造 600 行结构相同的数据，
保证测试快速、结果可精确断言（行号 i 与取值存在确定性函数关系）。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

# 项目根加入 sys.path（保证 app/config 包可导入）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# JAVA_HOME 由 app/utils/spark.py 自动探测（环境变量 / 候选路径）。
# 如需指定，请在 .env 设置 ANALYTICS_SPARK_JAVA_HOME。

import pytest
from pyspark.sql import SparkSession

from app import create_app
from app.data.data_provider import MemoryDataProvider, SPARCS_SCHEMA
from config.settings import testing_settings

N = 600  # 样本行数

_AGE_GROUPS = ["0 to 17", "18 to 29", "30 to 49", "50 to 69", "70 or Older"]
_GENDERS = ["Male", "Female"]
_ADMISSIONS = ["Emergency", "Elective", "Urgent", "Newborn"]
_PAYMENTS = ["Medicare", "Medicaid", "Private Health Insurance"]
_SEVERITY = ["Minor", "Moderate", "Major", "Extreme"]
_MORTALITY = ["Minor", "Moderate", "Major", "Extreme"]
_COUNTIES = ["Bronx", "Kings", "Queens"]
_MED_SURG = ["Medical", "Surgical"]


def _row(i: int, rnd: random.Random) -> tuple:
    """按确定性规则生成第 i 行，全部 33 列与 SPARCS_SCHEMA 顺序一致。"""
    age = _AGE_GROUPS[i % 5]
    gender = _GENDERS[i % 2]
    admission = _ADMISSIONS[i % 4]
    diag = f"DISEASE_{i % 6}"              # 6 类疾病
    proc = f"PROC_{i % 3 if i % 6 != 3 else 2}"  # 与疾病存在部分确定关联
    severity = _SEVERITY[i % 4]
    mortality = _MORTALITY[i % 4]
    return (
        # ---- 27 个字符串字段 ----
        "New York City", _COUNTIES[i % 3], f"OC{i:07d}", f"PF{i:06d}",
        f"FACILITY_{i % 8}", age, f"{100 + i % 30}", gender,
        "White", "Not Span/Hispanic", admission,
        "Home or Self Care" if i % 5 else "Expired",
        f"DX{i:03d}", diag, f"PR{i:03d}", proc,
        f"{i % 100:03d}", f"DRG_{i % 10}", f"{i % 25:02d}", f"MDC_{i % 20}",
        severity, mortality, _MED_SURG[i % 2],
        _PAYMENTS[i % 3], "Unknown", "Unknown",
        "Y" if i % 2 == 0 else "N",
        # ---- 4 个整型字段 ----
        (i % 20) + 1,                        # length_of_stay: 1~20
        2021,                                # discharge_year
        i % 4,                               # severity code: 0~3
        None if i % 10 == 0 else 3000 + i,   # birth_weight（10% 为空）
        # ---- 2 个浮点字段 ----
        1000.0 + i * 10.0,                   # total_charges
        600.0 + i * 4.0,                     # total_costs
    )


@pytest.fixture(scope="session")
def spark():
    session = (
        SparkSession.builder
        .master("local[2]")
        .appName("analytics-service-test")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "4")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    # 不清空：会话级复用，pytest 结束后进程退出自动回收


@pytest.fixture(scope="session")
def sample_df(spark):
    rnd = random.Random(42)
    rows = [_row(i, rnd) for i in range(N)]
    df = spark.createDataFrame(rows, schema=SPARCS_SCHEMA)
    return df.cache()


@pytest.fixture(scope="session")
def app(sample_df, tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("logs")
    settings = testing_settings(tmp_dir)
    provider = MemoryDataProvider(sample_df, row_count=N)
    return create_app(settings, data_provider=provider)


@pytest.fixture()
def client(app):
    return app.test_client()
