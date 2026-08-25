# -*- coding: utf-8 -*-
"""SparkSession 构建：负责处理 JAVA_HOME / HADOOP_HOME 探测等环境兼容问题。

兼容性：
  * Spark 3.5.1 官方兼容 Java 8/11/17（系统默认 Java 21 不兼容）；
  * 本模块在 Windows / Linux / macOS 下都能自动探测 JDK，候选路径见下方
    _JAVA_HOME_CANDIDATES；如需强制指定，设置环境变量 JAVA_HOME，
    或在 .env 中配置 ANALYTICS_SPARK_JAVA_HOME。
  * Windows 下 Spark 启动前必须解析 HADOOP_HOME 并设置 JVM 属性
    hadoop.home.dir，否则 Hadoop Shell 在 getWinUtilsPath() 抛异常。
    Hadoop 3.x 官方二进制移除了 winutils.exe，本模块自动探测已有
    HADOOP_HOME；如系统已安装 Hadoop 3.x 但缺 winutils，则改用
    纯 Python 的 pandas 路径（MySQL CSV DataProvider 回退）。
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

# 候选 HADOOP_HOME 路径（Windows 专用；Linux/macOS 一般通过环境变量设置）
_HADOOP_HOME_CANDIDATES_WIN: list[str] = [
    r"C:\Program Files\hadoop-3.5.0",
    r"C:\Program Files\hadoop-3.4.1",
    r"C:\Program Files\hadoop-3.4.0",
    r"C:\Program Files\hadoop-3.3.6",
    r"C:\Program Files\hadoop-3.3.5",
    r"C:\Program Files\hadoop",
    r"C:\Program Files\Hadoop",
    r"C:\hadoop",
    r"C:\winutils",
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


def _resolve_hadoop_home(settings: Settings) -> str | None:
    """按优先级解析 HADOOP_HOME：设置项 -> 环境变量 -> Windows 候选路径探测。

    Windows 上 Spark 启动 Hadoop Shell 时需要 hadoop.home.dir 指向含
    bin/winutils.exe 的目录。若返回 None，则应改用 pandas 纯 Python 路径。
    """
    hadoop_home = settings.spark_hadoop_home.strip() if getattr(settings, "spark_hadoop_home", "") else ""
    if hadoop_home and Path(hadoop_home).exists():
        return hadoop_home

    env_home = os.environ.get("HADOOP_HOME", "") or os.environ.get("hadoop.home.dir", "")
    if env_home and Path(env_home).exists():
        return env_home

    if sys.platform == "win32":
        import glob
        for candidate in _HADOOP_HOME_CANDIDATES_WIN:
            for matched in glob.glob(candidate):
                p = Path(matched)
                # 合法 HADOOP_HOME: 至少有 bin/ 子目录 + etc/hadoop/
                if (p / "bin").exists() and (p / "etc" / "hadoop").exists():
                    return str(p)
    return None


def _get_short_path_if_needed(path: str) -> str:
    """若 Windows 路径包含空格，返回其 8.3 短路径；否则原样返回。

    原因：spark.driver.extraJavaOptions 按空格切分参数，
    `-Dhadoop.home.dir=C:/Program Files/hadoop-3.5.0` 会被解析为两段，
    第二段 `Files/hadoop-3.5.0` 被 JVM 当作类名导致 ClassNotFoundException。
    """
    if sys.platform != "win32" or " " not in path:
        return path
    try:
        import ctypes
        from ctypes import wintypes

        _GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
        _GetShortPathNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        _GetShortPathNameW.restype = wintypes.DWORD

        # 第一次调用：返回需要的缓冲区大小
        length = _GetShortPathNameW(path, None, 0)
        if length == 0:
            return path  # 获取失败，返回原路径
        buf = ctypes.create_unicode_buffer(length)
        if _GetShortPathNameW(path, buf, length) == 0:
            return path
        return buf.value
    except Exception:
        # 获取失败时回退到 Windows Scripting Host（部分精简系统没有 ctypes）
        try:
            import win32api  # type: ignore
            return win32api.GetShortPathName(path)
        except Exception:
            return path


def _pyarrow_available() -> bool:
    """检测 pyarrow 是否可用（Arrow 优化需要）。"""
    import importlib.util
    return importlib.util.find_spec("pyarrow") is not None


# ---------------------------------------------------------------------------
# 原生数学库（OpenBLAS / LAPACK）探测：
#   Windows 下 Spark ML（breeze -> netlib-java）找不到原生 BLAS 时会回退到
#   慢速 Java 实现并刷 WARN。解决方式：把含 openblas/lapack DLL 的目录注入
#   spark.driver.extraLibraryPath / spark.executor.extraLibraryPath。
#   本机无需额外安装：venv 里的 numpy/scipy wheel 自带 libscipy_openblas64_*.dll。
# 可用 ANALYTICS_SPARK_EXTRA_LIBRARY_PATH 强制指定（多个路径用分号分隔）。
# ---------------------------------------------------------------------------
def _dir_has_blas(d: Path) -> bool:
    """目录下是否存在 openblas/lapack 动态库。"""
    try:
        for p in d.iterdir():
            name = p.name.lower()
            if "openblas" in name or name.startswith(("liblapack", "lapack")):
                return True
    except OSError:
        pass
    return False


def _detect_native_blas_dirs(extra: str = "") -> list[str]:
    """探测本机可用的 OpenBLAS/LAPACK DLL 目录（显式配置优先）。"""
    dirs: list[str] = []
    # 1) 显式配置优先（支持分号分隔多个）
    if extra and extra.strip():
        dirs.extend(p for p in (s.strip() for s in extra.split(";") if s.strip()))
    candidates: list[Path] = []
    # 2) 当前 Python 环境的 numpy wheel 自带 OpenBLAS
    try:
        import numpy as _np
        numpy_dir = Path(_np.__file__).resolve().parent
        candidates.append(numpy_dir.parent / "numpy.libs")            # pip wheel 布局
        candidates.append(numpy_dir / ".libs")                        # 备选布局
        candidates.append(numpy_dir.parent / "Library" / "bin")       # conda win 布局
    except Exception:  # noqa: BLE001
        pass
    # 3) scipy wheel 同样自带
    try:
        import scipy as _sp
        candidates.append(Path(_sp.__file__).resolve().parent.parent / "scipy.libs")
    except Exception:  # noqa: BLE001
        pass
    for c in candidates:
        if c.is_dir() and str(c) not in dirs and _dir_has_blas(c):
            dirs.append(str(c))
    return dirs


# Windows 下 Spark 创建本地临时目录依赖 winutils.exe（hadoop.dll），
# 若 HADOOP_HOME 未设置会报 "HADOOP_HOME and hadoop.home.dir are unset"
_HADOOP_HOME_CANDIDATES: list[str] = [
    r"D:\Project_env\hadoop-bin",
    r"C:\hadoop",
    r"D:\hadoop",
]


def _ensure_hadoop_home() -> str | None:
    """Windows 下确保 HADOOP_HOME 指向含 bin\\winutils.exe 的目录；非 Windows 返回 None。"""
    if sys.platform != "win32":
        return None

    env_home = os.environ.get("HADOOP_HOME", "").strip()
    if env_home and Path(env_home, "bin", "winutils.exe").exists():
        return env_home

    for candidate in _HADOOP_HOME_CANDIDATES:
        if Path(candidate, "bin", "winutils.exe").exists():
            return candidate
    return None


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

    # Windows 必须设置 HADOOP_HOME + hadoop.home.dir，
    # 否则 Hadoop Shell 在 <clinit> 中因找不到 winutils.exe 直接抛异常。
    # 这里同时设置 os.environ（JVM 启动时读）和 Spark 配置（JVM 系统属性）。
    hadoop_home = _resolve_hadoop_home(settings)
    if sys.platform == "win32" and hadoop_home:
        os.environ["HADOOP_HOME"] = hadoop_home
        logger.info("Spark 环境：HADOOP_HOME=%s（已设置 JVM hadoop.home.dir）", hadoop_home)
    else:
        logger.info("Spark 环境：HADOOP_HOME=%s（非 Windows 或未探测到，跳过）",
                    hadoop_home or "<unset>")

    logger.info("Spark 环境：JAVA_HOME=%s，PYSPARK_PYTHON=%s，master=%s",
                java_home, sys.executable, settings.spark_master)

    # Windows 下注入 HADOOP_HOME（winutils.exe），避免 SparkContext 初始化失败
    hadoop_home = _ensure_hadoop_home()
    if hadoop_home:
        os.environ["HADOOP_HOME"] = hadoop_home
        os.environ["hadoop.home.dir"] = hadoop_home
        bin_dir = str(Path(hadoop_home, "bin"))
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        logger.info("Spark 环境：HADOOP_HOME=%s", hadoop_home)
    elif sys.platform == "win32":
        logger.warning("未找到 winutils.exe（HADOOP_HOME），Spark 在 Windows 上可能无法创建本地临时目录")

    builder = (
        SparkSession.builder
        .appName(settings.app_name)
        .master(settings.spark_master)
        .config("spark.driver.memory", settings.spark_driver_memory)
        .config("spark.sql.shuffle.partitions", settings.spark_shuffle_partitions)
        # MySQL JDBC 驱动（Spark 读取 MySQL 所需）
        .config("spark.jars.packages", "com.mysql:mysql-connector-j:8.4.0")
        # 二期 3.3.4：Spark SQL AQE 自适应执行（动态合并 shuffle 分区，减少小任务开销）
        .config("spark.sql.adaptive.enabled",
                "true" if settings.spark_adaptive_enabled else "false")
        .config("spark.sql.adaptive.coalescePartitions.enabled",
                "true" if settings.spark_adaptive_enabled else "false")
        # Arrow 优化（安装 pyarrow 时启用）：本地数据转换与 collect 在驱动进程完成，
        # 兼容性更好、速度更快；未安装时回退到 Python worker 路径
        .config("spark.sql.execution.arrow.pyspark.enabled",
                "true" if _pyarrow_available() else "false")
        .config("spark.sql.execution.arrow.pyspark.fallback.enabled", "true")
        .config("spark.sql.session.timeZone", "UTC")
        # 本地模式关闭 UI 端口冲突告警，允许多进程测试
        .config("spark.ui.enabled", "true" if settings.env != "testing" else "false")
    )
    # 关键：必须在 SparkContext 初始化前把 hadoop.home.dir 注入 JVM 系统属性，
    # 否则 Hadoop Shell 仍会用默认 HADOOP_HOME 检查导致 HADOOP_HOME unset。
    # 注意：
    #   (1) Windows 路径反斜杠在 JVM 参数中会被吃掉，必须全部替换为正斜杠。
    #   (2) 路径不能有空格：spark.driver.extraJavaOptions 按空格切分参数，
    #       "C:/Program Files/..." 会被拆成两段，导致 JVM 把 "Files/..."
    #       当作类名抛出 ClassNotFoundException。因此使用 Windows 8.3 短路径
    #       代替，如 C:\PROGRA~1\HADOOP~1.0（Scripting.FileSystemObject 可获取）。
    #   (3) 不能加引号，引号会被当作路径一部分。
    if sys.platform == "win32" and hadoop_home:
        os.environ["HADOOP_HOME_WARN_SUPPRESS"] = "true"
        hadoop_home_jvm = _get_short_path_if_needed(hadoop_home).replace("\\", "/")
        builder = builder.config(
            "spark.driver.extraJavaOptions",
            f"-Dhadoop.home.dir={hadoop_home_jvm}",
        )

    # 原生数学库注入（openblas/lapack）：修复 Spark ML 在 Windows 上
    # 找不到原生 BLAS 导致的 WARN 与慢速回退。driver 与 executor 都要配。
    native_dirs = _detect_native_blas_dirs(
        getattr(settings, "spark_extra_library_path", "") or "")
    if native_dirs:
        lib_path = os.pathsep.join(native_dirs).replace("\\", "/")
        builder = (
            builder
            .config("spark.driver.extraLibraryPath", lib_path)
            .config("spark.executor.extraLibraryPath", lib_path)
        )
        logger.info("Spark 环境：extraLibraryPath=%s（openblas/lapack）", lib_path)
    else:
        logger.info("Spark 环境：未探测到 openblas/lapack 原生库，Spark ML 使用 Java 回退实现")

    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel(settings.spark_log_level)
    return spark
