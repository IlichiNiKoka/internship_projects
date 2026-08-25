# -*- coding: utf-8 -*-
"""数据层：DataProvider 抽象 + Spark CSV 实现 + 测试用内存实现。

设计要点：
  * 服务层/算法层只依赖 DataProvider.dataframe()，不关心数据来自 Spark CSV、
    Hive 还是测试内存 DataFrame —— 换数据源不动业务代码；
  * SparkDataProvider 惰性加载 + cache()：首次查询触发一次全表扫描，
    后续聚合全部命中内存缓存，大幅降低重复查询延迟；
  * 显式 schema：按清洗后数据字典定义类型，读取速度与稳定性均优于推断。

注意：
  * Windows 下 Spark / Hadoop Shell 在 JVM 初始化前必须已设置 HADOOP_HOME
    和 JAVA_HOME，否则 pyspark 模块导入时会启动 Java gateway 导致崩溃。
  * 本模块在模块顶部通过 app.utils.spark._resolve_* 预设置环境变量，
    确保在 import pyspark 之前环境已就绪。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

# ---------------------------------------------------------------------------
# 【关键】在任何 pyspark 导入之前预先设置 JAVA_HOME / HADOOP_HOME。
# pyspark.sql 的顶层导入会触发 PySpark 内部的 SparkContext._ensure_initialized
# 路径，可能创建 Java gateway；此时环境变量必须正确设置，
# 否则 Windows 平台将出现 HADOOP_HOME unset 或 Java gateway 崩溃。
# ---------------------------------------------------------------------------
from app.utils.spark import _resolve_hadoop_home, _resolve_java_home  # noqa: E402
from config.settings import Settings  # noqa: E402

try:
    _settings = Settings.load()
except Exception:  # settings 加载失败（如测试环境）时跳过环境预配置
    _settings = None

if _settings is not None:
    try:
        _java_home = _resolve_java_home(_settings)
        os.environ["JAVA_HOME"] = _java_home
    except Exception:
        pass
    try:
        _hadoop_home = _resolve_hadoop_home(_settings)
        if _hadoop_home:
            os.environ["HADOOP_HOME"] = _hadoop_home
            if sys.platform == "win32":
                os.environ["HADOOP_HOME_WARN_SUPPRESS"] = "true"
    except Exception:
        pass

from pyspark.sql import DataFrame, SparkSession, functions as F  # noqa: E402
from pyspark.sql.types import (  # noqa: E402
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)

from app.core.exceptions import ServiceUnavailableError  # noqa: E402

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


def _cast_to_schema(df: DataFrame) -> DataFrame:
    """按 SPARCS_SCHEMA 逐列 cast 类型并重排为标准字段顺序。

    CSV / MySQL / HDFS 三种数据源读取后的列顺序或类型可能不一致，
    统一在这里对齐：按 schema 字段名 cast（脏值容错为 null），
    再 select 出标准顺序的 33 个字段。
    """
    for field in SPARCS_SCHEMA.fields:
        df = df.withColumn(field.name, F.col(field.name).cast(field.dataType))
    return df.select(*[f.name for f in SPARCS_SCHEMA.fields])


def _read_csv_and_cast(spark: SparkSession, path: str) -> DataFrame:
    """按“列名”读取 CSV（首行表头）并对齐到标准 schema。

    清洗文件列顺序可能与 SPARCS_SCHEMA 不同（例如 length_of_stay 可能在
    前面或后面），因此先整表按字符串读入，再按 schema 列名逐列 cast。
    Spark 本地 CSV 与 HDFS CSV 复用同一逻辑。
    """
    df = (
        spark.read
        .option("header", "true")
        .option("inferSchema", "false")
        .csv(path)
    )
    return _cast_to_schema(df)


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
        self._lock = threading.Lock()

    def dataframe(self) -> DataFrame:
        # 双重检查锁：前端大屏首次打开会并发多个聚合请求，若不互斥，
        # 会同时触发全量加载（200 万行），Spark local 模式下互相抢资源卡死。
        if self._df is None:
            with self._lock:
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
            df = _read_csv_and_cast(spark, csv_path.as_posix())
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


class MySQLDataProvider(DataProvider):
    """从 MySQL 读取已入库数据（人员2 storage.loader 产物 · 二期数据底座）。

    通过 Spark JDBC 读取 `db_table` 表，只保留 33 个业务字段（跳过 id / row_hash
    两个系统列），按 SPARCS_SCHEMA 统一列名与类型后缓存全表，供聚合/算法层使用。
    连接失败统一转 ServiceUnavailableError（503），由中间件标准化响应。
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._df: DataFrame | None = None
        self._row_count: int | None = None
        self._load_seconds: float | None = None
        self._lock = threading.Lock()

    def dataframe(self) -> DataFrame:
        # 双重检查锁：防止前端大屏并发请求同时触发 MySQL 全表加载（200 万行）
        if self._df is None:
            with self._lock:
                if self._df is None:
                    self._load()
        return self._df

    def _jdbc_url(self) -> str:
        s = self._settings
        return f"jdbc:mysql://{s.db_host}:{s.db_port}/{s.db_name}"

    def _try_load_parquet_snapshot(self) -> bool:
        """尝试直接读 MySQL 落盘的 Parquet 快照（列式 + Snappy，免 JDBC 全表拉取）。

        成功返回 True 并完成 self._df 物化；快照不存在/损坏返回 False，
        由调用方回退到 JDBC 加载并在加载后重建快照。
        """
        s = self._settings
        if not getattr(s, "parquet_snapshot_enabled", True):
            return False
        snap = Path(s.data_parquet_path)
        if not snap.exists():
            return False
        start = time.perf_counter()
        try:
            t0 = time.perf_counter()
            spark = _get_or_create_spark(s)
            t1 = time.perf_counter()
            df = spark.read.parquet(snap.as_posix())
            # 快照由 _write_parquet_snapshot 写出，列名/类型/顺序已对齐 SPARCS_SCHEMA，
            # 无需再做一遍逐列 cast（省一次全表投影）。
            expected = [f.name for f in SPARCS_SCHEMA.fields]
            if list(df.columns) != expected:
                logger.info("Parquet 快照列与标准 schema 不一致，执行对齐 cast")
                df = _cast_to_schema(df)
            df = df.cache()
            self._row_count = df.count()   # 触发读取并物化缓存
            t2 = time.perf_counter()
            self._df = df
            self._load_seconds = round(time.perf_counter() - start, 2)
            logger.info(
                "Parquet 快照加载完成: %d 行，总耗时 %.2fs（Spark 启动 %.2fs + 读取/物化 %.2fs）",
                self._row_count, self._load_seconds, t1 - t0, t2 - t1)
            return True
        except Exception as exc:  # noqa: BLE001 —— 快照损坏时回退 JDBC 重建
            logger.warning("Parquet 快照不可用（%s），回退 MySQL JDBC 全量加载", exc)
            self._df = None
            return False

    def _write_parquet_snapshot(self, df: DataFrame) -> None:
        """把已对齐 schema 的 DataFrame 写为 Parquet 快照（overwrite，Snappy）。失败不阻塞。"""
        s = self._settings
        if not getattr(s, "parquet_snapshot_enabled", True):
            return
        snap = Path(s.data_parquet_path)
        try:
            snap.parent.mkdir(parents=True, exist_ok=True)
            df.write.mode("overwrite").option("compression", "snappy").parquet(snap.as_posix())
            logger.info("MySQL -> Parquet 快照落盘完成: %s", snap)
        except Exception as exc:  # noqa: BLE001 —— 落盘失败不影响本次服务
            logger.warning("Parquet 快照写入失败（下次启动仍走 JDBC）: %s", exc)

    def _load(self) -> None:
        # 快路径：已有 Parquet 快照则直读（消除 ~30s 的 JDBC 冷启动加载）
        if self._try_load_parquet_snapshot():
            return
        start = time.perf_counter()
        url = self._jdbc_url()
        logger.info("开始从 MySQL 加载数据: %s (table=%s)",
                    url, self._settings.db_table)
        try:
            spark = _get_or_create_spark(self._settings)
            s = self._settings
            # 分区读取：先查询自增主键 id 上界，按 id 分片并行加载，
            # 避免单分区全表扫描 200 万行导致耗时超过接口超时（本地实测约 234s > 120s）
            max_id = None
            try:
                max_id = (
                    spark.read.format("jdbc")
                    .option("url", url)
                    .option("dbtable", f"(SELECT MAX(id) AS m FROM {s.db_table}) t")
                    .option("user", s.db_user)
                    .option("password", s.db_password)
                    .option("driver", s.mysql_jdbc_driver)
                    .option("connectTimeout", str(s.mysql_jdbc_connect_timeout_ms))
                    .load()
                    .collect()[0][0]
                )
            except Exception:  # 老表无 id 主键时忽略，退回单分区加载
                logger.warning("MySQL 分区读取不可用（无 id 主键？），退回单分区加载")
            reader = (
                spark.read.format("jdbc")
                .option("url", url)
                .option("dbtable", self._settings.db_table)
                .option("user", self._settings.db_user)
                .option("password", self._settings.db_password)
                .option("driver", self._settings.mysql_jdbc_driver)
                .option("connectTimeout", str(self._settings.mysql_jdbc_connect_timeout_ms))
            )
            if max_id is not None:
                reader = (
                    reader
                    .option("partitionColumn", "id")
                    .option("lowerBound", "1")
                    .option("upperBound", str(int(max_id) + 1))
                    .option("numPartitions", "8")
                )
            df = reader.load()
            # 入库表含 id / row_hash 系统列，此处只取 33 个业务字段并对齐标准 schema
            df = _cast_to_schema(df)
            df = df.cache()
            self._row_count = df.count()       # 触发 JDBC 读取并物化缓存
            self._df = df
            self._load_seconds = round(time.perf_counter() - start, 2)
            logger.info("MySQL 数据加载完成: %d 行，耗时 %.2fs",
                        self._row_count, self._load_seconds)
            # 首次 JDBC 加载后落盘 Parquet 快照，后续启动直读（消除冷启动开销）
            if getattr(self._settings, "parquet_snapshot_enabled", True):
                self._write_parquet_snapshot(df)
        except ServiceUnavailableError:
            raise
        except Exception as exc:  # JDBC 驱动缺失 / 连接失败 / SQL 异常统一转 503
            logger.exception("MySQL 数据加载失败")
            raise ServiceUnavailableError(
                message=f"MySQL 数据加载失败: {exc}",
                detail={"jdbc_url": url, "table": self._settings.db_table},
            ) from exc

    def status(self) -> dict:
        s = self._settings
        return {
            "data_source": (
                f"mysql://{s.db_host}:{s.db_port}/{s.db_name}/{s.db_table}"
            ),
            "spark_master": s.spark_master,
            "loaded": self._df is not None,
            "row_count": self._row_count,
            "load_seconds": self._load_seconds,
        }


class HDFSDataProvider(DataProvider):
    """从 HDFS 读取清洗后 CSV（或 Parquet）数据（二期数据底座）。

    HDFS 地址由 `hdfs_namenode` + `hdfs_path` 拼装，Spark 原生支持 hdfs:// 协议，
    无需额外客户端；读取后同样对齐 SPARCS_SCHEMA 并缓存全表。
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._df: DataFrame | None = None
        self._row_count: int | None = None
        self._load_seconds: float | None = None
        self._lock = threading.Lock()

    def dataframe(self) -> DataFrame:
        # 双重检查锁：防止前端大屏并发请求同时触发 HDFS 全量加载
        if self._df is None:
            with self._lock:
                if self._df is None:
                    self._load()
        return self._df

    def _hdfs_url(self) -> str:
        s = self._settings
        namenode = s.hdfs_namenode or "hdfs://localhost:8020"
        path = s.hdfs_path or "/data/sparcs_clean.csv"
        return f"{namenode.rstrip('/')}/{path.lstrip('/')}"

    def _load(self) -> None:
        start = time.perf_counter()
        url = self._hdfs_url()
        logger.info("开始从 HDFS 加载数据: %s", url)
        try:
            spark = _get_or_create_spark(self._settings)
            if url.endswith(".parquet"):
                df = spark.read.parquet(url)
                df = _cast_to_schema(df)
            else:
                df = _read_csv_and_cast(spark, url)
            df = df.cache()
            self._row_count = df.count()       # 触发 HDFS 读取并物化缓存
            self._df = df
            self._load_seconds = round(time.perf_counter() - start, 2)
            logger.info("HDFS 数据加载完成: %d 行，耗时 %.2fs",
                        self._row_count, self._load_seconds)
        except ServiceUnavailableError:
            raise
        except Exception as exc:  # NameNode 不可达 / 文件不存在统一转 503
            logger.exception("HDFS 数据加载失败")
            raise ServiceUnavailableError(
                message=f"HDFS 数据加载失败: {exc}",
                detail={"hdfs_url": url},
            ) from exc

    def status(self) -> dict:
        return {
            "data_source": self._hdfs_url(),
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


def build_data_provider(settings: Settings) -> DataProvider:
    """按 `data_source` 配置构建数据提供者（二期：csv / mysql / hdfs 数据底座）。

    * csv    -> SparkDataProvider（本地清洗后 CSV，一期默认）
    * mysql  -> MySQLDataProvider（读取人员2 入库的 MySQL 表）
    * hdfs   -> HDFSDataProvider（读取 HDFS 上的清洗后数据）

    业务代码（服务层 / 算法层）只依赖 DataProvider.dataframe()，
    换数据源无需改动，只需在 .env 切换 ANALYTICS_DATA_SOURCE。
    """
    source = (getattr(settings, "data_source", "csv") or "csv").strip().lower()
    if source == "csv":
        return SparkDataProvider(settings)
    if source == "mysql":
        return MySQLDataProvider(settings)
    if source == "hdfs":
        return HDFSDataProvider(settings)
    raise ServiceUnavailableError(
        message=f"不支持的数据源配置: {source}（可选 csv / mysql / hdfs）",
        detail={"data_source": source},
    )
