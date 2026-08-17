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

    # ---- Spark ----
    spark_master: str = "local[*]"          # 生产可改为 yarn / spark://...
    spark_log_level: str = "WARN"           # Spark 内部日志级别
    spark_driver_memory: str = "2g"
    spark_java_home: str = ""               # 留空自动探测（见 utils/spark.py）
    spark_shuffle_partitions: int = 8       # 本地模式无需过多分区

    # ---- 结果缓存（一期：进程内 TTL 缓存；二期切换 Redis，接口不变）----
    cache_enabled: bool = True
    cache_ttl_seconds: int = 300
    cache_max_entries: int = 256

    # ---- 聚合接口限制 ----
    agg_default_limit: int = 100
    agg_max_limit: int = 1000
    agg_max_dimensions: int = 5             # 单次查询最多组合维度数

    # ---- 机器学习 ----
    ml_sample_size: int = 100_000           # 费用预测训练样本上限（平衡精度与耗时）
    ml_train_ratio: float = 0.8
    ml_seed: int = 42

    # ---- 日志 ----
    log_dir: Path = LOG_DIR_DEFAULT
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, env: str | None = None) -> "Settings":
        """从 .env 与环境变量加载配置。"""
        load_dotenv(BASE_DIR / ".env", override=False)

        kwargs: dict = {}
        for f in fields(cls):
            raw = os.environ.get(f"ANALYTICS_{f.name.upper()}")
            if raw is None:
                continue
            if f.default is not MISSING:
                default = f.default
            elif f.default_factory is not MISSING:
                default = f.default_factory()
            else:
                default = None
            if f.type is bool:
                kwargs[f.name] = _parse_bool(raw, bool(default))
            elif f.type is int:
                kwargs[f.name] = int(raw)
            elif f.type is float:
                kwargs[f.name] = float(raw)
            elif f.type is Path:
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
