# -*- coding: utf-8 -*-
"""集中配置：运行参数、路径、Spark、缓存、机器学习等。

设计原则：
  1. 所有“可能随环境变化”的魔法值集中在本文件，业务代码不写死路径/端口；
  2. 支持 .env 文件 + 系统环境变量（ANALYTICS_ 前缀）覆盖默认值，
     覆盖优先级：环境变量 > .env > 代码默认值；
  3. 分层取值：默认面向 development，测试通过 Settings(test=...) 注入。
"""

from __future__ import annotations

import os
import typing
from dataclasses import MISSING, dataclass, fields
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 路径推导（基于本文件位置，不写死盘符，方便迁移）
# config/settings.py -> data_process/（扁平化后的项目根）
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent      # data_process/
LOG_DIR_DEFAULT = BASE_DIR / "logs"

# 清洗后数据默认路径（由数据清洗流水线产出，位于本项目的 processed/ 目录）
DEFAULT_CLEAN_CSV = (
    BASE_DIR
    / "processed"
    / "Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012_clean.csv"
)


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """服务全局配置。实例化后请勿直接改属性，通过 load() 或构造参数注入。"""

    # ---- 应用 ----
    env: str = "development"                # development / testing / production
    app_name: str = "medical-analytics-service"
    version: str = "1.0.0"
    host: str = "127.0.0.1"
    port: int = 5000
    debug: bool = False

    # ---- 数据源 ----
    data_csv_path: Path = DEFAULT_CLEAN_CSV
    # 数据提供者类型：csv（本地清洗后 CSV，一期）/ mysql / hdfs（二期数据底座）
    data_source: str = "csv"
    # MySQL 数据底座：复用人员2 入库的 db_* 连接参数，走 Spark JDBC 读取入库表
    mysql_jdbc_driver: str = "com.mysql.cj.jdbc.Driver"   # MySQL Connector/J 驱动类
    mysql_jdbc_connect_timeout_ms: int = 5000             # JDBC 建连超时（毫秒）
    # HDFS 数据底座
    hdfs_namenode: str = ""                 # NameNode 地址，如 hdfs://namenode:8020（留空用默认）
    hdfs_path: str = ""                     # HDFS 上清洗后数据路径，如 /data/sparcs_clean.csv
    # MySQL -> Parquet 列式快照（性能优化）：首次 JDBC 全量读取后落盘为 Parquet，
    # 后续启动直接读 Parquet（Snappy 列存，免 JDBC 拉取 200 万行），消除 ~30s 冷启动加载。
    parquet_snapshot_enabled: bool = True
    data_parquet_path: Path = BASE_DIR / "processed" / "sparcs_snapshot.parquet"

    # ---- 结构化大数据入库（人员2 · 二期 MySQL / SQLite 兜底）----
    # engine: auto(MySQL 优先，失败降级 SQLite) / mysql / sqlite
    db_engine: str = "auto"
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "medical_analytics"
    db_table: str = "sparcs_discharge_2021"
    db_batch_size: int = 10000          # 每批写入行数（批量 INSERT，避免单条过慢）
    db_sqlite_path: Path = DEFAULT_CLEAN_CSV.parent / "sparcs.db"  # SQLite 兜底文件

    # ---- Spark ----
    spark_master: str = "local[*]"          # 生产可改为 yarn / spark://...
    spark_log_level: str = "WARN"           # Spark 内部日志级别
    spark_driver_memory: str = "2g"
    spark_java_home: str = ""               # 留空自动探测（见 utils/spark.py）
    spark_hadoop_home: str = ""             # 留空自动探测（Windows 下必填路径含 bin/winutils.exe）
    spark_shuffle_partitions: int = 8       # 本地模式无需过多分区
    spark_extra_library_path: str = ""      # 显式指定 openblas/lapack 原生库目录（留空自动探测）
    spark_adaptive_enabled: bool = True     # Spark SQL AQE 自适应执行（二期任务参数优化）

    # ---- 结果缓存（一期：进程内 TTL 缓存；二期支持 Redis 后端）----
    cache_enabled: bool = True
    cache_backend: str = "in-memory"       # in-memory / redis（redis 不可用时自动降级）
    cache_ttl_seconds: int = 300
    cache_max_entries: int = 256
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_connect_timeout: float = 2.0     # 连接/读写超时（秒）
    # 后端启动时自动拉起 Redis Docker 容器（见 run.py::_ensure_redis_via_docker）
    redis_autostart: bool = True
    redis_container: str = "medical-redis"   # 容器名（不存在时会以该名新建）
    redis_image: str = "redis:7-alpine"      # 新建容器使用的镜像

    # ---- 超时与慢查询（二期 3.3.4 API 性能优化）----
    agg_timeout_seconds: float = 120.0     # 聚合计算超时阈值，<=0 表示不限制
    algo_timeout_seconds: float = 300.0    # 算法计算超时阈值，<=0 表示不限制
    slow_query_threshold_seconds: float = 5.0  # 超过该耗时的请求记入慢查询并告警

    # ---- 权限与限流（二期 3.3.5 API 异常处理机制）----
    api_auth_enabled: bool = False         # 是否启用 API Token 认证
    api_auth_tokens: str = ""              # 逗号分隔的合法 Token；启用认证但留空则拒绝所有请求
    api_auth_public_paths: str = "/api/v1/health"  # 免认证路径前缀（逗号分隔）
    rate_limit_enabled: bool = False       # 是否启用限流
    rate_limit_requests: int = 100         # 滑动窗口内单个客户端允许的最大请求数
    rate_limit_window_seconds: int = 60    # 滑动窗口长度（秒）

    # ---- 聚合接口限制 ----
    agg_default_limit: int = 100
    agg_max_limit: int = 1000
    agg_max_dimensions: int = 5             # 单次聚合最多组合维度数

    # ---- 启动预热 ----
    # 服务启动后在后台线程预加载 Spark 数据源（Parquet 快照 ~20s），
    # 让首个用户请求不必承担冷启动开销；testing 环境自动跳过。
    warmup_on_startup: bool = True             # 单次查询最多组合维度数

    # ---- 机器学习 ----
    ml_sample_size: int = 100_000           # 费用预测训练样本上限（平衡精度与耗时）
    ml_train_ratio: float = 0.8
    ml_seed: int = 42

    # ---- AI 智能层（人员4：意图识别 / 文本生成）----
    # LLM 提供方：auto(按 api_key 是否配置自动选) / openai / deepseek / mock / disabled
    llm_provider: str = "auto"
    llm_api_key: str = ""                  # 留空时降级到 Mock 生成，不报错
    # Base URL 留空时：provider=deepseek 用 https://api.deepseek.com/v1；
    # provider=openai 用官方；自建/本地 LLM 在 .env 显式填 http://localhost:8000/v1
    llm_base_url: str = ""
    # 默认模型：DeepSeek-V3（OpenAI 兼容接口）。可改为 deepseek-reasoner（R1）或 gpt-4o-mini 等
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 30                  # 单次 LLM 调用超时（秒）
    # 重试策略（二期优化）：重试次数 + 总预算双重限制，避免慢端点阻塞用户几十秒
    llm_max_retries: int = 1               # 失败后最大重试次数
    llm_retry_budget_seconds: float = 45.0  # 含首次调用在内的总耗时预算（秒），超出即放弃降级 Mock
    llm_ollama_think: bool = False         # provider=ollama 时是否开启思考链（关闭可大幅降低延迟）
    llm_extra_body: str = ""               # OpenAI 兼容端点额外请求体（JSON），如 {"reasoning_effort": "none"}
    llm_temperature: float = 0.2           # 低温度减少幻觉
    llm_max_tokens: int = 4000             # 文本生成最大 token（DeepSeek V4 Flash 需更大 token 防止思考内容被截断）
    # 意图识别：达标阈值（低于此值时分类器输出 unsupported）
    intent_min_confidence: float = 0.45

    # ---- 日志 ----
    log_dir: Path = LOG_DIR_DEFAULT
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, env: str | None = None) -> "Settings":
        """从 .env 与环境变量加载配置。"""
        load_dotenv(BASE_DIR / ".env", override=False)

        # 文件顶部 `from __future__ import annotations` 会把字段注解变成字符串，
        # 因此必须用 get_type_hints 解析真实类型，否则 bool/int/float/Path 全判为 str
        type_hints = typing.get_type_hints(cls)

        kwargs: dict = {}
        for f in fields(cls):
            raw = os.environ.get(f"ANALYTICS_{f.name.upper()}")
            if raw is None:
                continue
            ftype = type_hints.get(f.name)
            if ftype is bool:
                kwargs[f.name] = _parse_bool(raw, False)
            elif ftype is int:
                kwargs[f.name] = int(raw)
            elif ftype is float:
                kwargs[f.name] = float(raw)
            elif ftype is Path:
                kwargs[f.name] = Path(raw)
            else:
                kwargs[f.name] = raw
        if env is not None:
            kwargs["env"] = env
        return cls(**kwargs)


# 测试用最小配置（tests/conftest.py 中再细化）
def testing_settings(tmp_dir: Path, data_csv: Path | None = None) -> Settings:
    return Settings(
        env="testing",
        debug=False,
        data_csv_path=data_csv or DEFAULT_CLEAN_CSV,
        spark_master="local[2]",
        spark_log_level="ERROR",
        cache_enabled=False,
        agg_default_limit=50,
        agg_max_limit=100,
        ml_sample_size=2_000,
        log_level="WARNING",
        log_dir=tmp_dir,
    )


# pytest 默认按 test_* 收集测试函数，testing_settings 名字前缀也匹配，
# 显式标记不收集，避免被误识别为测试函数
testing_settings.__test__ = False  # type: ignore[attr-defined]
