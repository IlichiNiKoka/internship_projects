# -*- coding: utf-8 -*-
"""数据层：DataProvider 抽象 + Spark CSV 实现 + 测试用内存实现。

设计要点：
  * 服务层/算法层只依赖 DataProvider.dataframe()，不关心数据来自 Spark CSV、
    Hive 还是测试内存 DataFrame —— 换数据源不动业务代码；
  * SparkDataProvider 惰性加载 + cache()：首次查询触发一次全表扫描，
    后续聚合全部命中内存缓存，大幅降低重复查询延迟；
  * 显式 schema：按清洗后数据字典定义类型，读取速度与稳定性均优于推断。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import (
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)

from app.core.exceptions import ServiceUnavailableError
from config.settings import Settings

logger = logging.getLogger(__name__)

# 清洗后数据字典 -> Spark schema（见 data_dictionary.md，33 字段）
_STRING_FIELDS = [
    "hospital_service_area", "hospital_county", "operating_certificate_number",
    "permanent_facility_id", "facility_name", "age_group", "zip_code_3_digits",
    "gender", "race", "ethnicity", "type_of_admission", "patient_disposition",
    "ccsr_diagnosis_code", "ccsr_diagnosis_description", "ccsr_procedure_code",
    "ccsr_procedure_description", "apr_drg_code", "apr_drg_description",
    "apr_mdc_code", "apr_mdc_description", "apr_severity_of_illness_description",
    "apr_risk_of_mortality", "apr_medical_surgical_description",
    "payment_typology_1", "payment_typology_2", "payment_typology_3",
    "emergency_department_indicator",
]
_INT_FIELDS = [
    "length_of_stay", "discharge_year", "apr_severity_of_illness_code", "birth_weight",
]
_DOUBLE_FIELDS = ["total_charges", "total_costs"]

SPARCS_SCHEMA = StructType(
    [StructField(name, StringType(), True) for name in _STRING_FIELDS]
    + [StructField(name, IntegerType(), True) for name in _INT_FIELDS]
    + [StructField(name, DoubleType(), True) for name in _DOUBLE_FIELDS]
)


def _schema_from_csv_header(csv_path) -> StructType:
    """按 CSV 实际表头顺序构建 Spark Schema（列对齐修复）。

    背景：Spark 在显式提供 schema 时按“列位置”解析 CSV（enforceSchema 默认
    true），不会用表头名称匹配字段。若 schema 字段顺序与表头列顺序不一致，
    整表数据会错位（例如 type_of_admission 读到住院天数、length_of_stay 读到
    支付方式而全部为 null）。

    这里先读取文件表头，再按表头顺序用“字段名 -> 类型”字典映射出 schema，
    与清洗流水线产出的列顺序解耦，任何列顺序下都能正确对齐。
    """
    import csv as csv_module

    type_by_name = {f.name: f.dataType for f in SPARCS_SCHEMA.fields}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        header = next(csv_module.reader(fh))

    fields: list[StructField] = []
    for raw in header:
        name = raw.strip()
        data_type = type_by_name.get(name)
        if data_type is None:
            logger.warning("CSV 表头含未知列，已忽略: %s", name)
            continue
        fields.append(StructField(name, data_type, True))
    if len(fields) != len(SPARCS_SCHEMA.fields):
        logger.warning("CSV 表头列数(%d)与 Schema 字段数(%d)不一致，请核对数据文件",
                       len(fields), len(SPARCS_SCHEMA.fields))
    return StructType(fields)


class DataProvider(ABC):
    """数据提供者统一接口。"""

    @abstractmethod
    def dataframe(self) -> DataFrame:
        """返回全量清洗后数据 DataFrame（已在实现内部按需缓存）。"""

    @abstractmethod
    def status(self) -> dict:
        """数据源健康状态（行数、加载耗时等）。"""


class SparkDataProvider(DataProvider):
    """从清洗后 CSV 加载数据（Spark 本地/集群均可）。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._df: DataFrame | None = None
        self._row_count: int | None = None
        self._load_seconds: float | None = None

    def dataframe(self) -> DataFrame:
        if self._df is None:
            self._load()
        return self._df

    def _load(self) -> None:
        csv_path = self._settings.data_csv_path
        if not csv_path.exists():
            raise ServiceUnavailableError(
                message=f"清洗后数据文件不存在: {csv_path}（请先运行数据预处理流水线）",
                detail={"path": str(csv_path)},
            )
        start = time.perf_counter()
        logger.info("开始加载数据: %s (master=%s)", csv_path, self._settings.spark_master)
        try:
            spark = _get_or_create_spark(self._settings)
            # 按表头顺序构建 schema，避免列错位（见 _schema_from_csv_header 说明）
            schema = _schema_from_csv_header(csv_path)
            df = (
                spark.read
                .option("header", "true")
                .schema(schema)
                .csv(csv_path.as_posix())
            )
            df = df.cache()                    # 缓存全表，后续聚合复用
            self._row_count = df.count()       # 触发加载并物化缓存
            self._df = df
            self._load_seconds = round(time.perf_counter() - start, 2)
            logger.info("数据加载完成: %d 行，耗时 %.2fs", self._row_count, self._load_seconds)
        except ServiceUnavailableError:
            raise
        except Exception as exc:  # Spark 启动/读取失败统一转 503
            logger.exception("数据加载失败")
            raise ServiceUnavailableError(
                message=f"数据加载失败: {exc}",
                detail={"path": str(csv_path)},
            ) from exc

    def status(self) -> dict:
        return {
            "data_source": str(self._settings.data_csv_path),
            "spark_master": self._settings.spark_master,
            "loaded": self._df is not None,
            "row_count": self._row_count,
            "load_seconds": self._load_seconds,
        }


class MemoryDataProvider(DataProvider):
    """测试/演示用：直接注入内存 DataFrame（无需真实 CSV）。"""

    def __init__(self, df: DataFrame, row_count: int | None = None):
        self._df = df.cache()
        self._row_count = row_count if row_count is not None else self._df.count()

    def dataframe(self) -> DataFrame:
        return self._df

    def status(self) -> dict:
        return {
            "data_source": "memory",
            "spark_master": self._df.sparkSession.sparkContext.master,
            "loaded": True,
            "row_count": self._row_count,
            "load_seconds": 0.0,
        }


def _get_or_create_spark(settings: Settings) -> SparkSession:
    """延迟导入以支持测试环境（app/utils/spark.py 负责 JAVA_HOME 探测）。"""
    from app.utils.spark import build_spark_session
    return build_spark_session(settings)
