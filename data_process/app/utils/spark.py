# -*- coding: utf-8 -*-
"""SparkSession 构建：负责处理 JAVA_HOME 探测等环境兼容问题。

兼容性：
  * Spark 3.5.1 官方兼容 Java 8/11/17（系统默认 Java 21 不兼容）；
  * 本模块在 Windows / Linux / macOS 下都能自动探测 JDK，候选路径见下方
    _JAVA_HOME_CANDIDATES；如需强制指定，设置环境变量 JAVA_HOME，
    或在 .env 中配置 ANALYTICS_SPARK_JAVA_HOME。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from pyspark.sql import SparkSession

from config.settings import Settings

logger = logging.getLogger(__name__)

# 候选 JDK 8/11/17 路径（按顺序探测，命中即用，跨平台）
_JAVA_HOME_CANDIDATES: list[str] = [
    # ---- Linux 常见路径 ----
    "/usr/lib/jvm/java-17-openjdk-amd64",
    "/usr/lib/jvm/java-17-openjdk",
    "/usr/lib/jvm/java-11-openjdk-amd64",
    "/usr/lib/jvm/java-11-openjdk",
    "/usr/lib/jvm/java-8-openjdk-amd64",
    "/usr/lib/jvm/temurin-17*",
    "/usr/lib/jvm/temurin-11*",
    "/usr/lib/jvm/adoptopenjdk-17*",
    # ---- macOS Homebrew / 标准 JDK 安装 ----
    "/Library/Java/JavaVirtualMachines/jdk-17*",
    "/Library/Java/JavaVirtualMachines/jdk-11*",
    "/Library/Java/JavaVirtualMachines/temurin-17*",
    "/Library/Java/JavaVirtualMachines/adoptopenjdk-17*",
    # ---- Windows 常见路径 ----
    r"C:\Program Files\Java\jdk-17",
    r"C:\Program Files\Java\jdk-11",
    r"C:\Program Files\Java\jdk1.8.0_*",
    r"C:\Program Files\Eclipse Adoptium\jdk-17*",
    r"C:\Program Files\Eclipse Adoptium\jdk-11*",
]


def _resolve_java_home(settings: Settings) -> str:
    """按优先级解析 JAVA_HOME：配置项 -> 环境变量 -> 候选路径探测。"""
    java_home = settings.spark_java_home.strip() if settings.spark_java_home else ""
    if java_home and Path(java_home).exists():
        return java_home

    env_home = os.environ.get("JAVA_HOME", "")
    if env_home and Path(env_home).exists():
        return env_home

    import glob
    for candidate in _JAVA_HOME_CANDIDATES:
        for matched in glob.glob(candidate):
            p = Path(matched)
            # 有效的 JAVA_HOME 应包含 bin/java 或 bin/java.exe
            if (p / "bin" / "java.exe").exists() or (p / "bin" / "java").exists():
                return str(p)
    raise RuntimeError(
        "未找到兼容的 JAVA_HOME（Spark 3.5 需要 Java 8/11/17）。"
        "请设置环境变量 JAVA_HOME，或在 .env 中配置 ANALYTICS_SPARK_JAVA_HOME。"
    )


def build_spark_session(settings: Settings) -> SparkSession:
    """构建（或复用）SparkSession，统一环境变量与常用参数。

    幂等：进程内仅创建一个 Session，多处调用返回同一实例。
    """
    existing = SparkSession.getActiveSession()
    if existing is not None:
        return existing

    java_home = _resolve_java_home(settings)
    os.environ["JAVA_HOME"] = java_home
    # 关键：让 Spark worker 使用当前 Python 解释器，
    # 避免 PYSPARK_PYTHON 指向已卸载/其它环境导致 ImportError。
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable

    logger.info("Spark 环境：JAVA_HOME=%s，PYSPARK_PYTHON=%s，master=%s",
                java_home, sys.executable, settings.spark_master)

    spark = (
        SparkSession.builder
        .appName(settings.app_name)
        .master(settings.spark_master)
        .config("spark.driver.memory", settings.spark_driver_memory)
        .config("spark.sql.shuffle.partitions", settings.spark_shuffle_partitions)
        .config("spark.sql.session.timeZone", "UTC")
        # 本地模式关闭 UI 端口冲突告警，允许多进程测试
        .config("spark.ui.enabled", "true" if settings.env != "testing" else "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel(settings.spark_log_level)
    return spark
