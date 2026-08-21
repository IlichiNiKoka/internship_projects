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
from typing import get_type_hints

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

    # ---- AI 智能层（人员4：意图识别 / 文本生成）----
    # LLM 提供方：auto(按 api_key 是否配置自动选) / openai / deepseek / mock / disabled
    llm_provider: str = "auto"
    llm_api_key: str = ""                  # 留空时降级到 Mock 生成，不报错
    # Base URL 留空时：provider=deepseek 用 https://api.deepseek.com/v1；
    # provider=openai 用官方；自建/本地 LLM 在 .env 显式填 http://localhost:8000/v1
    llm_base_url: str = ""
    # 默认模型：DeepSeek V4 Flash（OpenAI 兼容接口）。
    # 旧名 deepseek-chat / deepseek-reasoner 已于 2026-07-24 弃用；
    # 也可改为 deepseek-v4-pro 或 gpt-4o-mini 等其他 OpenAI 兼容模型
    llm_model: str = "deepseek-v4-flash"
    llm_timeout: int = 30                  # 单次 LLM 调用超时（秒）
    llm_temperature: float = 0.2           # 低温度减少幻觉
    llm_max_tokens: int = 4000          # 文本生成最大 token（含思考内容；deepseek-v4-flash 会先输出 reasoning，预算过小会导致 content 为空）
    # 意图识别：达标阈值（低于此值时分类器输出 unsupported）
    intent_min_confidence: float = 0.45
    # 文本生成幻觉检查：原文数字与生成文本数字允许的相对误差
    hallucination_tolerance: float = 0.02

    # ---- AI 应用编排（人员1：工具 / 会话 / 报告）----
    # 单体默认进程内调用人员3服务；拆分部署可切换 http。
    analysis_api_mode: str = "local"        # local / http
    analysis_api_base_url: str = ""          # http 模式必填
    analysis_api_key: str = ""               # 可选服务间凭证，不写入会话/报告
    analysis_api_timeout: float = 30.0
    # 对前端暴露的 /assistant 共享访问凭证；开发环境可留空，
    # 生产环境必须配置，支持 Bearer 或 X-Assistant-API-Key。
    assistant_api_key: str = ""

    tool_max_attempts: int = 3
    tool_retry_base_seconds: float = 0.1
    tool_retry_max_seconds: float = 1.0

    # 开发无 Redis 地址时使用有界内存；生产环境由构建器强制 Redis。
    conversation_backend: str = "auto"       # auto / memory / redis
    conversation_ttl_seconds: int = 86_400
    conversation_max_sessions: int = 1_000
    conversation_max_messages: int = 100
    conversation_max_analyses: int = 20
    conversation_max_reports: int = 10
    conversation_max_result_rows: int = 200
    redis_url: str = ""
    redis_key_prefix: str = "medical:conversation:"
    redis_socket_timeout: float = 1.0
    # 分布式会话锁必须覆盖最慢的 Spark/LLM 任务，并在持锁期间定期续租。
    conversation_lock_timeout_seconds: float = 900.0
    conversation_lock_blocking_timeout_seconds: float = 30.0
    conversation_lock_renew_interval_seconds: float = 60.0

    report_max_analyses: int = 10

    # ---- 日志 ----
    log_dir: Path = LOG_DIR_DEFAULT
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, env: str | None = None) -> "Settings":
        """从 .env 与环境变量加载配置。"""
        load_dotenv(BASE_DIR / ".env", override=False)

        kwargs: dict = {}
        # ``from __future__ import annotations`` 会让 Field.type 可能是字符串；
        # 先解析真实类型，确保端口、TTL、布尔值等环境变量不会错误保留为 str。
        type_hints = get_type_hints(cls)
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
            target_type = type_hints.get(f.name, f.type)
            if target_type is bool:
                kwargs[f.name] = _parse_bool(raw, bool(default))
            elif target_type is int:
                kwargs[f.name] = int(raw)
            elif target_type is float:
                kwargs[f.name] = float(raw)
            elif target_type is Path:
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
