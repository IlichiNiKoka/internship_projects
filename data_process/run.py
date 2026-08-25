# -*- coding: utf-8 -*-
"""开发环境启动入口：python run.py

生产环境请使用 wsgi.py + gunicorn（Linux），或 Flask 生产级部署方案。

关键：Windows 下 Spark / Hadoop 依赖 JAVA_HOME、HADOOP_HOME 环境变量，
必须在任何 import pyspark（以及任何 transitively 导入 data_provider / ai
服务层的代码）之前完成解析并注入。因此本文件在顶部先加载 settings，
再调用 utils/spark.py 的探测函数设置环境变量，最后才导入 app。
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import time

# Windows 控制台中文兼容
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 【非常关键】Spark/JAVA_HOME / HADOOP_HOME 环境变量 bootstrap：
#   必须在导入 app / data_provider / pyspark 之前设置。
#   utils/spark.py 负责探测，本文件只负责确保在导入链路最前面执行。
# ---------------------------------------------------------------------------
from config.settings import Settings

_settings_bootstrap = Settings.load()

try:
    from app.utils.spark import _resolve_hadoop_home, _resolve_java_home

    try:
        _java = _resolve_java_home(_settings_bootstrap)
        os.environ["JAVA_HOME"] = _java
    except Exception:
        pass

    try:
        _hadoop = _resolve_hadoop_home(_settings_bootstrap)
        if _hadoop:
            os.environ["HADOOP_HOME"] = _hadoop
            if sys.platform == "win32":
                os.environ["HADOOP_HOME_WARN_SUPPRESS"] = "true"
    except Exception:
        pass
except Exception:
    # utils/spark.py 本身导入失败时（如 pyspark 未安装）不阻塞启动，
    # 等真正触发 Spark 加载时再报明确错误。
    pass

from app import create_app  # noqa: E402


# ---------------------------------------------------------------------------
# Redis Docker 容器自动启停：
#   * main() 开头调用 _ensure_redis_via_docker()：6379 不可达则自动
#     docker start（容器不存在则按 settings.redis_image 新建）；
#   * 进程退出时（Ctrl+C / 正常结束，经 atexit）自动 docker stop，
#     实现后端停、Redis 停。taskkill /F 强杀不会触发 atexit。
#   * Docker 未运行/未安装只告警不阻塞：缓存层会自动降级为进程内缓存。
# ---------------------------------------------------------------------------
def _redis_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _ensure_redis_via_docker(settings) -> str | None:
    """确保 Redis 可用，并返回本服务接管的容器名（不接管则返回 None）。

    接管规则：名为 settings.redis_container 的容器存在，即视为由后端管理 ——
    无论它是已在运行还是已停止，后端退出时都会 docker stop 它。
    """
    if not getattr(settings, "redis_autostart", True):
        return None
    host, port = settings.redis_host, int(settings.redis_port)
    container = getattr(settings, "redis_container", "medical-redis") or "medical-redis"
    image = getattr(settings, "redis_image", "redis:7-alpine") or "redis:7-alpine"

    # Docker 本身不可用时直接放弃（缓存层稍后会自动降级为进程内缓存）
    if _docker("info").returncode != 0:
        if not _redis_reachable(host, port):
            print("  * Redis    : Docker 不可用且 6379 无响应 —— 跳过自启，缓存降级为进程内")
        return None

    # 容器是否存在（存在即接管其生命周期）
    exists = _docker("inspect", "-f", "{{.State.Running}}", container).returncode == 0

    if _redis_reachable(host, port):
        if exists:
            return container      # 我们的容器已在跑：接管，退出时停
        print(f"  * Redis    : {host}:{port} 已有非 Docker 容器的实例在跑，不接管")
        return None

    # 端口不通 -> 拉起：容器存在但停止则 start，否则按镜像新建
    if exists:
        started = _docker("start", container)
    else:
        started = _docker(
            "run", "-d", "--name", container,
            "-p", f"127.0.0.1:{port}:6379",
            "--restart", "unless-stopped",
            "--health-cmd", "redis-cli ping",
            "--health-interval", "10s", "--health-retries", "5",
            image, "redis-server", "--appendonly", "no",
        )
    if started.returncode != 0:
        print(f"  * Redis    : 容器 {container} 启动失败："
              f"{(started.stderr or '').strip().splitlines()[-1:]}")
        return None

    for _ in range(15):          # 最多等 15s 健康就绪
        if _redis_reachable(host, port):
            print(f"  * Redis    : 已通过 Docker 拉起 {container} ({host}:{port})")
            return container
        time.sleep(1)
    print(f"  * Redis    : 容器 {container} 已启动但端口未就绪，继续启动（可能降级）")
    return container


def _compose_dir(settings) -> Path:
    """定位 deploy/docker-compose.yml 所在目录（默认项目根 ../deploy）。"""
    from pathlib import Path

    custom = getattr(settings, "infra_compose_dir", "") or ""
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent.parent / "deploy"


def _ensure_datastore_via_compose(settings) -> dict:
    """通过根目录 deploy/docker-compose.yml 拉起 MySQL / HDFS 数据底座容器。

    与 Redis 不同：MySQL/HDFS 承载持久化数据，后端退出时**不**停容器
    （生命周期交给 Docker restart 策略），避免每次启动都重新加载数据。
    Docker 不可用时只告警不阻塞：后续连接失败会由数据层报 503。

    返回实际拉起的服务名列表（用于启动日志展示）。
    """
    started: list[str] = []
    compose = _compose_dir(settings)
    if not (compose / "docker-compose.yml").exists():
        print(f"  * 数据底座 : 未找到 {compose}/docker-compose.yml，跳过自启")
        return started

    services = []
    if getattr(settings, "mysql_autostart", True):
        services.append("medical-mysql")
    if getattr(settings, "hdfs_autostart", True):
        services.append("medical-hdfs")
    if not services:
        return started

    # Docker 本身不可用时直接放弃
    if _docker("info").returncode != 0:
        print("  * 数据底座 : Docker 不可用 —— 跳过 MySQL/HDFS 自启")
        return started

    result = _docker(
        "compose", "-f", str(compose / "docker-compose.yml"),
        "up", "-d", *services,
    )
    if result.returncode != 0:
        print(f"  * 数据底座 : docker compose up 失败："
              f"{(result.stderr or '').strip().splitlines()[-1:]}")
        return started
    started.extend(services)

    # 等待端口就绪（首次导入 .ibd/dump 可能较久，这里只做有限等待）
    for svc, port in (("medical-mysql", int(settings.db_port)),
                      ("medical-hdfs", 8020)):
        for _ in range(30):          # 最多等 150s
            if _redis_reachable("127.0.0.1", port):
                break
            time.sleep(5)
    print(f"  * 数据底座 : 已通过 docker compose 拉起 {'/'.join(services)}")
    return started


def _stop_redis_on_exit(container: str | None) -> None:
    """后端退出时优雅停掉由本进程接管的 Redis 容器。

    三重保障（幂等，只停一次）：
      * atexit：正常结束 / KeyboardInterrupt 兑底；
      * SIGINT / SIGBREAK 信号处理器：Windows 控制台 Ctrl+C、taskkill 等
        发出的中断默认硬杀进程，atexit 不会执行，这里显式接管。
    """
    if not container:
        return

    state = {"done": False}

    def _shutdown() -> None:
        if state["done"]:
            return
        state["done"] = True
        result = _docker("stop", container)
        if result.returncode == 0:
            print(f"\n  [OK] Redis 容器 {container} 已随服务停止")
        else:
            print(f"\n  [WARN] Redis 容器 {container} 停止失败，可手动执行: docker stop {container}")

    def _on_signal(signum, frame):
        _shutdown()
        # 先停容器再退出；用 os._exit 避免再次进入解释器关闭流程重复处理
        print("\n  [OK] 服务已停止")
        os._exit(128 + signum)

    atexit.register(_shutdown)
    import signal
    for sig_name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass   # 非 主线程 / 平台不支持时忽略


def main() -> None:
    settings = Settings.load()

    # MySQL / HDFS 数据底座自启（在 create_app 之前：DataProvider 会连库）
    datastore = _ensure_datastore_via_compose(settings)

    # Redis 自启（在 create_app 之前：build_cache 会立刻 ping Redis）
    redis_container = _ensure_redis_via_docker(settings)
    _stop_redis_on_exit(redis_container)   # 后端停，Redis 停

    app = create_app(settings)
    print(f"\n  {settings.app_name} v{settings.version}")
    print(f"  * 运行环境 : {settings.env}")
    print(f"  * 数据源   : {settings.data_source}"
          f"（{settings.db_host}:{settings.db_port}/{settings.db_name}"
          f"/{settings.db_table}）")
    print(f"  * Spark    : {settings.spark_master}")
    if os.environ.get("HADOOP_HOME"):
        print(f"  * Hadoop   : {os.environ['HADOOP_HOME']}")
    if redis_container:
        print(f"  * Redis    : docker:{redis_container}"
              f"（{settings.redis_host}:{settings.redis_port}，随服务退出自动停止）")
    if datastore:
        print(f"  * 数据底座 : docker compose:{','.join(datastore)}（随 Docker 常驻，退出不停）")
    print(f"  * 服务地址 : http://{settings.host}:{settings.port}")
    print(f"  * 健康检查 : http://{settings.host}:{settings.port}/api/v1/health\n")
    app.run(host=settings.host, port=settings.port, debug=settings.debug,
            threaded=True)


if __name__ == "__main__":
    main()
