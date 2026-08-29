# -*- coding: utf-8 -*-
"""开发环境启动入口：python run.py

生产环境请使用 wsgi.py + gunicorn（Linux），或 Flask 生产级部署方案。

关键：Windows 下 Spark / Hadoop 依赖 JAVA_HOME、HADOOP_HOME 环境变量，
必须在任何 import pyspark（以及任何 transitively 导入 data_provider / ai
服务层的代码）之前完成解析并注入。因此本文件在顶部先加载 settings，
再调用 utils/spark.py 的探测函数设置环境变量，最后才导入 app。

一键启停（本文件职责）：
  * 启动：经 deploy/docker-compose.yml 拉起 MySQL / HDFS / Redis 全部底座
    容器（compose 不可用时回退为仅独立拉起 Redis）；llm_provider=ollama 时
    自动拉起本地 Ollama 服务并确保模型存在（缺失自动 pull）。
  * 停止：Ctrl+C / 正常退出时，自动停止本进程拉起的 Ollama，以及全部接管
    的底座容器（infra_stop_on_exit=false 可让容器常驻）。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

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
# 常量
# ---------------------------------------------------------------------------
OLLAMA_API = "http://localhost:11434"

# 容器名 == compose service 名（deploy/docker-compose.yml 中显式 container_name）
HDFS_RPC_PORT = 8020


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _port_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _compose_dir(settings) -> Path:
    """定位 deploy/docker-compose.yml 所在目录（默认项目根 ../deploy）。"""
    custom = str(getattr(settings, "infra_compose_dir", "") or "").strip()
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parent.parent / "deploy"


def _compose_file(settings) -> Path:
    return _compose_dir(settings) / "docker-compose.yml"


def _docker_compose_project(name: str) -> str | None:
    """读取容器上的 compose 项目标签；非 compose 管理的容器返回 None。"""
    r = _docker(
        "inspect", "-f", "{{index .Config.Labels \"com.docker.compose.project\"}}", name
    )
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


# ---------------------------------------------------------------------------
# 容器：compose 一键启动（MySQL / HDFS / Redis）
# ---------------------------------------------------------------------------
def _reconcile_legacy_redis(compose_project: str) -> None:
    """兼容旧版 run.py：存在非 compose 管理的 medical-redis 时移除重建。

    Redis 是纯缓存容器，删除不丢任何持久数据；避免与 compose up 创建同名
    容器冲突，并保证退出时能被统一管理（docker stop 按容器名生效）。
    """
    project = _docker_compose_project("medical-redis")
    if project is None or project == compose_project:
        return
    removed = _docker("rm", "-f", "medical-redis")
    if removed.returncode == 0:
        print("  * Redis    : 移除旧版独立 Redis 容器（非 compose 管理），由 compose 统一重建")
    else:
        print("  * Redis    : 旧版独立 Redis 容器移除失败，compose 启动可能冲突")


def _ensure_infra_via_compose(settings) -> list[str]:
    """通过 deploy/docker-compose.yml 拉起 MySQL / HDFS / Redis 全部底座容器。

    返回本次「接管」的服务名列表（退出时统一 docker stop）。
    compose 不可用时不阻塞：main() 会回退到 _ensure_redis_via_docker。
    """
    started: list[str] = []
    if not _compose_file(settings).exists():
        return started

    services = []
    if getattr(settings, "mysql_autostart", True):
        services.append("medical-mysql")
    if getattr(settings, "hdfs_autostart", True):
        services.append("medical-hdfs")
    if getattr(settings, "redis_autostart", True):
        services.append("medical-redis")
    if not services:
        return started

    # Docker 本身不可用时直接放弃（Redis 回退路径同样会失败并告警）
    if _docker("info").returncode != 0:
        print("  * 容器     : Docker 不可用 —— 跳过 MySQL/HDFS/Redis 自启"
              "（缓存降级为进程内）")
        return started

    _reconcile_legacy_redis(_compose_dir(settings).name or "deploy")

    result = _docker(
        "compose", "-f", str(_compose_file(settings)),
        "up", "-d", "--no-recreate", "--build", *services,
    )
    if result.returncode != 0:
        print(f"  * 容器     : docker compose up 失败："
              f"{(result.stderr or '').strip().splitlines()[-1:]}")
        return started
    started.extend(services)

    # 等待端口就绪（首次 mysql-init 导入可能较久，统一预算轮询）
    ports = {
        "medical-mysql": int(settings.db_port),
        "medical-hdfs": HDFS_RPC_PORT,
        "medical-redis": int(settings.redis_port),
    }
    deadline = time.time() + float(getattr(settings, "infra_startup_wait", 180.0))
    pending = list(started)
    while pending and time.time() < deadline:
        for svc in list(pending):
            if _port_reachable("127.0.0.1", ports[svc]):
                pending.remove(svc)
        if pending:
            time.sleep(2)
    for svc in pending:
        print(f"  * 容器     : {svc} 端口 {ports[svc]} 未在预算时间内就绪，"
              f"继续启动（后端会按需重试；可 docker logs {svc} 查看）")
    print(f"  * 容器     : 已通过 docker compose 拉起 {'/'.join(started)}")
    return started


def _ensure_redis_via_docker(settings) -> str | None:
    """compose 不可用时的回退：独立 docker run 拉起 Redis 并接管生命周期。

    接管规则：名为 settings.redis_container 的容器存在即视为由后端管理 ——
    无论它是否在运行，退出时都会 docker stop 它。
    """
    if not getattr(settings, "redis_autostart", True):
        return None
    host, port = settings.redis_host, int(settings.redis_port)
    container = getattr(settings, "redis_container", "medical-redis") or "medical-redis"
    image = getattr(settings, "redis_image", "redis:7-alpine") or "redis:7-alpine"

    # Docker 本身不可用时直接放弃（缓存层稍后会自动降级为进程内缓存）
    if _docker("info").returncode != 0:
        if not _port_reachable(host, port):
            print("  * Redis    : Docker 不可用且 6379 无响应 —— 跳过自启，缓存降级为进程内")
        return None

    # 容器是否存在（存在即接管其生命周期）
    exists = _docker("inspect", "-f", "{{.State.Running}}", container).returncode == 0

    if _port_reachable(host, port):
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
        if _port_reachable(host, port):
            print(f"  * Redis    : 已通过 Docker 拉起 {container} ({host}:{port})")
            return container
        time.sleep(1)
    print(f"  * Redis    : 容器 {container} 已启动但端口未就绪，继续启动（可能降级）")
    return container


# ---------------------------------------------------------------------------
# 本地 AI 模型：Ollama 服务与模型自启 / 自停
# ---------------------------------------------------------------------------
def _ollama_reachable(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ollama_models(timeout: float = 3.0) -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models") or []]
    except Exception:
        return []


def _ollama_bin(settings) -> str:
    return str(getattr(settings, "ollama_bin", "") or "").strip() or "ollama"


def _spawn_ollama_serve(settings, log_path: Path) -> subprocess.Popen:
    """后台拉起 ollama serve（无窗口），输出写入日志文件。"""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as logf:
        return subprocess.Popen(
            [_ollama_bin(settings), "serve"],
            stdout=logf, stderr=subprocess.STDOUT,
            creationflags=flags,
        )


def _ensure_ollama(settings) -> dict:
    """确保本地 Ollama 服务与配置的模型可用，返回生命周期句柄。

    句柄 {"started": bool, "proc": Popen|None}：只有本进程拉起的 serve
    才在退出时被停止；原本已在运行的 Ollama 不受影响。
    """
    handle = {"started": False, "proc": None}
    provider = str(getattr(settings, "llm_provider", "") or "").strip().lower()
    if provider != "ollama":
        print(f"  * AI 模型 : llm_provider={provider or '未配置'}，跳过本地模型自启")
        return handle
    if not getattr(settings, "llm_autostart", True):
        print("  * AI 模型 : llm_autostart=false，跳过本地模型自启")
        return handle

    model = str(getattr(settings, "llm_model", "") or "").strip()
    binary = _ollama_bin(settings)

    if _ollama_reachable():
        print("  * AI 模型 : Ollama 服务已在运行")
    else:
        if shutil.which(binary) is None:
            print(f"  * AI 模型 : 未找到 {binary} 可执行文件且 11434 无服务，"
                  f"跳过自启（AI 层将降级 Mock）")
            return handle
        log = Path(getattr(settings, "log_dir", Path("logs"))) / "ollama-serve.log"
        proc = _spawn_ollama_serve(settings, log)
        print(f"  * AI 模型 : 正在启动本地 Ollama（日志 {log}）...")
        deadline = time.time() + float(getattr(settings, "ollama_startup_wait", 90.0))
        while time.time() < deadline:
            if _ollama_reachable():
                break
            if proc.poll() is not None:
                break           # serve 已自行退出（端口被占等），交由下方统一判断
            time.sleep(1.0)
        if _ollama_reachable():
            handle["started"] = True
            handle["proc"] = proc
            print("  * AI 模型 : Ollama 服务已由本进程拉起（退出时自动停止）")
        else:
            _stop_ollama_proc(proc)
            print("  * AI 模型 : Ollama 启动失败/超时，请查看 logs/ollama-serve.log"
                  "（AI 层将降级 Mock）")
            return handle

    # 模型检查 / 缺失自动拉取
    if model:
        installed = _ollama_models()
        if model in installed:
            print(f"  * AI 模型 : 模型 {model} 已就绪")
        elif getattr(settings, "ollama_pull_on_missing", True):
            print(f"  * AI 模型 : 模型 {model} 未安装，开始拉取（首次约 2~4GB，请耐心等待）...")
            pulled = subprocess.run([binary, "pull", model])
            if pulled.returncode == 0:
                print(f"  * AI 模型 : 模型 {model} 拉取完成")
            else:
                print(f"  * AI 模型 : 模型 {model} 拉取失败，可稍后手动执行: "
                      f"{binary} pull {model}（AI 层将降级 Mock）")
        else:
            print(f"  * AI 模型 : 模型 {model} 未安装且 ollama_pull_on_missing=false，"
                  f"AI 层将降级 Mock")
    return handle


def _stop_ollama_proc(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        print("  [OK] 本地 Ollama 服务已随服务停止")
    except Exception as exc:
        print(f"  [WARN] 本地 Ollama 服务停止失败: {exc}")


def _stop_ollama(handle) -> None:
    if handle.get("started"):
        _stop_ollama_proc(handle.get("proc"))


# ---------------------------------------------------------------------------
# 前端（Vue3 大屏）自启 / 自停
# ---------------------------------------------------------------------------
def _npm_bin() -> str | None:
    """定位 npm 可执行文件（Windows 返回 npm.cmd 全路径，避免 shell 解析问题）。"""
    exe = shutil.which("npm")
    if exe is None and sys.platform == "win32":
        exe = shutil.which("npm.cmd")
    return exe


def _terminate_proc_tree(proc) -> None:
    """整树结束进程：npm 会派生 node 子进程（vite），必须连子进程一起停。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _ensure_frontend(settings) -> dict:
    """自动拉起前端（dev = vite dev server / preview = build + vite preview）。

    句柄 {"started": bool, "proc": Popen|None, "url": str, "mode": str}：
      只有本进程拉起的 npm 进程才在退出时被整树停止；
      端口已被占用（用户已自行启动前端）时跳过、不接管。
    """
    handle = {"started": False, "proc": None, "url": "", "mode": ""}
    if not getattr(settings, "frontend_enabled", True):
        print("  * 前端     : frontend_enabled=false，跳过前端自启")
        return handle

    mode = str(getattr(settings, "frontend_mode", "dev") or "dev").strip().lower()
    if mode not in ("dev", "preview"):
        print(f"  * 前端     : frontend_mode={mode} 非法（可选 dev / preview），跳过")
        return handle

    npm = _npm_bin()
    if npm is None:
        print("  * 前端     : 未找到 npm（需要 Node.js >= 20.19 或 >= 22.12），跳过前端自启"
              "（可手动执行: cd smart-medical-frontend && npm run dev）")
        return handle

    # 前端目录：显式配置优先，否则按 run.py 所在目录推导 ../smart-medical-frontend
    custom_dir = str(getattr(settings, "frontend_dir", "") or "").strip()
    root = (Path(custom_dir) if custom_dir
            else Path(__file__).resolve().parent.parent / "smart-medical-frontend")
    root = root.resolve()
    if not (root / "package.json").exists():
        print(f"  * 前端     : 未找到 {root}/package.json，跳过前端自启")
        return handle

    host = str(getattr(settings, "frontend_host", "127.0.0.1") or "127.0.0.1")
    port = int(getattr(settings, "frontend_port", 5173) or 5173)
    handle["mode"] = mode
    handle["url"] = f"http://{host}:{port}"

    if _port_reachable(host, port):
        print(f"  * 前端     : {handle['url']} 已有服务在运行，不接管")
        return handle

    # 依赖缺失时自动 npm install（node_modules 里没有 vite 即视为未安装）
    if (getattr(settings, "frontend_auto_install", True)
            and not (root / "node_modules" / "vite").exists()):
        print("  * 前端     : 未发现 node_modules，执行 npm install（首次较慢，请稍候）...")
        inst = subprocess.run([npm, "install"], cwd=root, capture_output=True, text=True)
        if inst.returncode != 0:
            tail = (inst.stderr or "").strip().splitlines()[-1:]
            print(f"  * 前端     : npm install 失败：{tail}，跳过前端自启")
            return handle

    log = Path(getattr(settings, "log_dir", Path("logs"))) / "frontend.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    popen_kwargs: dict = {}
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        popen_kwargs["creationflags"] = flags
    else:
        popen_kwargs["start_new_session"] = True

    cmd = [npm, "run", mode]
    if mode == "preview" and getattr(settings, "frontend_build_before_preview", True):
        # 先构建再预览：npm 脚本不支持 &&，交给系统 shell 解析
        if sys.platform == "win32":
            cmd = ["cmd", "/c", "npm run build && npm run preview"]
        else:
            cmd = ["sh", "-c", "npm run build && npm run preview"]
    with log.open("ab") as logf:
        proc = subprocess.Popen(cmd, cwd=root, stdout=logf, stderr=subprocess.STDOUT, **popen_kwargs)
    handle["proc"] = proc

    deadline = time.time() + float(getattr(settings, "frontend_startup_wait", 120.0))
    while time.time() < deadline:
        if _port_reachable(host, port):
            handle["started"] = True
            print(f"  * 前端     : 已启动（{mode} 模式），地址 {handle['url']}"
                  f"（日志 {log}，退出时自动停止）")
            return handle
        if proc.poll() is not None:
            break   # 进程已退出（端口被占 / 构建失败等），交由下方统一判断
        time.sleep(1.0)

    _terminate_proc_tree(proc)
    print(f"  * 前端     : 启动超时/失败，请查看 {log}（可手动执行: "
          f"cd smart-medical-frontend && npm run {mode}）")
    return handle


def _stop_frontend(handle) -> None:
    if handle.get("started"):
        _terminate_proc_tree(handle.get("proc"))
        print("  [OK] 前端服务已随服务停止")


# ---------------------------------------------------------------------------
# 分析服务应用：host 模式本地 Flask / container 模式 docker compose 拉起
# ---------------------------------------------------------------------------
def _ensure_app_via_compose(settings) -> str | None:
    """容器化运行：docker compose 构建并拉起分析服务应用容器（含底座）。

    方案A：宿主机零依赖（无需 JDK / Hadoop / winutils），应用镜像内
    Linux 环境直接运行 PySpark。返回被接管的容器名（退出时随服务停止）；
    失败返回 None（不阻塞前端启动）。
    """
    if not _compose_file(settings).exists():
        print("  * 应用     : 未找到 deploy/docker-compose.yml，无法以 container 模式启动")
        return None
    if _docker("info").returncode != 0:
        print("  * 应用     : Docker 不可用，无法以 container 模式启动"
              "（可改用 run_mode=host，或手动执行 docker compose up -d）")
        return None

    container = str(getattr(settings, "app_container", "medical-app") or "medical-app")
    print(f"  * 应用     : 正在构建并拉起 {container} 容器（首次构建约 5~10 分钟）...")
    result = _docker("compose", "-f", str(_compose_file(settings)),
                     "up", "-d", "--build", container)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-2:]
        print(f"  * 应用     : docker compose up {container} 失败：{tail}")
        return None

    # 等待容器健康（首次启动需预热数据：Parquet 快照缺失时从 MySQL 全量加载）
    deadline = time.time() + float(getattr(settings, "app_startup_wait", 600.0))
    while time.time() < deadline:
        status = _docker("inspect", "-f", "{{.State.Health.Status}}", container)
        state = (status.stdout or "").strip()
        if status.returncode == 0 and state == "healthy":
            print(f"  * 应用     : 分析服务容器已就绪 http://127.0.0.1:{settings.port}")
            return container
        # 容器已退出（启动崩溃）则放弃等待并给出日志
        running = _docker("inspect", "-f", "{{.State.Running}}", container)
        if (running.stdout or "").strip() != "true":
            logs = _docker("logs", "--tail", "40", container)
            print(f"  * 应用     : 容器 {container} 未在运行，最近日志：\n"
                  f"{(logs.stdout or logs.stderr or '').strip()[-2000:]}")
            return None
        time.sleep(5)
    print(f"  * 应用     : 等待 {container} 健康超时（{settings.app_startup_wait:.0f}s），"
          f"请查看: docker logs {container}")
    return container


# ---------------------------------------------------------------------------
# 退出清理：Ollama + 全部接管容器（幂等，只执行一次）
# ---------------------------------------------------------------------------
def _register_exit_handlers(
    settings,
    *,
    compose_managed: list[str],
    standalone_redis: str | None,
    ollama: dict,
    frontend: dict,
    app_container: str | None = None,
) -> None:
    """统一注册退出清理。

    三重保障（幂等，只停一次）：
      * atexit：正常结束 / KeyboardInterrupt 兜底；
      * SIGINT / SIGBREAK 信号处理器：Windows 控制台 Ctrl+C / taskkill 等
        发出的中断默认硬杀进程，atexit 不会执行，这里显式接管。
    """
    stop_names = list(dict.fromkeys(compose_managed))
    if standalone_redis and standalone_redis not in stop_names:
        stop_names.append(standalone_redis)
    if app_container and app_container not in stop_names:
        stop_names.append(app_container)

    state = {"done": False}

    def _shutdown() -> None:
        if state["done"]:
            return
        state["done"] = True
        print()
        # 1) 前端（只停本进程拉起的 npm/vite 进程）
        _stop_frontend(frontend)
        # 2) 本地 AI 模型（只停本进程拉起的 serve）
        _stop_ollama(ollama)
        # 3) 底座容器
        if not stop_names:
            print("  [OK] 服务已停止")
            return
        if not getattr(settings, "infra_stop_on_exit", True):
            print(f"  [SKIP] infra_stop_on_exit=false，容器保持运行：{'/'.join(stop_names)}")
            print("  [OK] 服务已停止")
            return
        # 按容器名 stop（兼容 compose 管理 / 旧版独立容器两种情况）
        for name in stop_names:
            result = _docker("stop", name)
            if result.returncode == 0:
                print(f"  [OK] 容器 {name} 已随服务停止")
            else:
                print(f"  [WARN] 容器 {name} 停止失败，可手动执行: docker stop {name}")
        print("  [OK] 服务已停止")

    def _on_signal(signum, frame):
        _shutdown()
        # 先停容器再退出；用 os._exit 避免再次进入解释器关闭流程重复处理
        os._exit(128 + signum)

    atexit.register(_shutdown)
    for sig_name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass   # 非 主线程 / 平台不支持时忽略


def main() -> None:
    settings = Settings.load()

    # 1) 底座容器：优先 compose 统一拉起（MySQL / HDFS / Redis）
    compose_managed: list[str] = []
    standalone_redis: str | None = None
    if _compose_file(settings).exists():
        compose_managed = _ensure_infra_via_compose(settings)
        # compose 拉起失败（插件缺失 / 配置错误等）时，Redis 回退为独立容器
        if not compose_managed:
            standalone_redis = _ensure_redis_via_docker(settings)
    else:
        print("  * 容器     : 未找到 deploy/docker-compose.yml，回退为仅自启 Redis")
        standalone_redis = _ensure_redis_via_docker(settings)

    # 2) 本地 AI 模型（llm_provider=ollama 时自动拉起）
    ollama = _ensure_ollama(settings)

    # 3) 前端（Vue3 大屏：dev / preview 模式自动拉起，退出时整树停止）
    frontend = _ensure_frontend(settings)

    # 3.5) 分析服务应用：host 模式本地 Flask；container 模式 docker compose 拉起容器
    run_mode = str(getattr(settings, "run_mode", "host") or "host").strip().lower()
    app_container: str | None = None
    if run_mode == "container":
        app_container = _ensure_app_via_compose(settings)

    # 4) 退出清理（Ctrl+C / 正常结束）
    _register_exit_handlers(
        settings,
        compose_managed=compose_managed,
        standalone_redis=standalone_redis,
        ollama=ollama,
        frontend=frontend,
        app_container=app_container,
    )

    # 容器化模式：应用由 docker compose 管理，本进程仅编排 + 阻塞等待退出
    if run_mode == "container":
        if app_container is None:
            print("\n  [ERROR] 容器化模式启动失败，请检查上方日志（前端已独立拉起不受影响）")
            return
        print(f"\n  {settings.app_name} v{settings.version}（容器化模式）")
        print(f"  * 分析服务 : http://{settings.host}:{settings.port}（docker 容器 {app_container}）")
        print(f"  * 健康检查 : http://{settings.host}:{settings.port}/api/v1/health")
        if frontend.get("url"):
            lifecycle = ("随服务退出自动停止" if frontend.get("started")
                         else "已有实例在运行/启动失败，退出不影响")
            print(f"  * 前端     : {frontend['url']}（{frontend.get('mode')} 模式，{lifecycle}）")
        print("  * 按 Ctrl+C 停止全部服务（含容器）\n")
        try:
            while True:
                # 短睡循环：Windows 下主线程长 sleep 无法被 CTRL_BREAK 唤醒，
                # 1s 分片让 Ctrl+C / Ctrl+Break 两种中断都能及时触发退出清理
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    app = create_app(settings)
    print(f"\n  {settings.app_name} v{settings.version}")
    print(f"  * 运行环境 : {settings.env}")
    print(f"  * 数据源   : {settings.data_source}"
          f"（{settings.db_host}:{settings.db_port}/{settings.db_name}"
          f"/{settings.db_table}）")
    print(f"  * Spark    : {settings.spark_master}")
    if os.environ.get("HADOOP_HOME"):
        print(f"  * Hadoop   : {os.environ['HADOOP_HOME']}")
    containers = list(dict.fromkeys(compose_managed + ([standalone_redis] if standalone_redis else [])))
    if containers:
        lifecycle = ("随服务退出自动停止" if getattr(settings, "infra_stop_on_exit", True)
                     else "常驻运行（infra_stop_on_exit=false）")
        print(f"  * 容器     : {'/'.join(containers)}（{lifecycle}）")
    if str(getattr(settings, "llm_provider", "")).strip().lower() == "ollama":
        ollama_state = ("随服务退出自动停止" if ollama.get("started")
                        else "已在运行，退出不受影响")
        print(f"  * AI 模型 : ollama:{settings.llm_model}"
              f"（{OLLAMA_API}，{ollama_state}）")
    if frontend.get("url"):
        lifecycle = ("随服务退出自动停止" if frontend.get("started")
                     else "已有实例在运行/启动失败，退出不影响")
        print(f"  * 前端     : {frontend['url']}（{frontend.get('mode')} 模式，{lifecycle}）")
    print(f"  * 服务地址 : http://{settings.host}:{settings.port}")
    print(f"  * 健康检查 : http://{settings.host}:{settings.port}/api/v1/health\n")
    app.run(host=settings.host, port=settings.port, debug=settings.debug,
            threaded=True)


if __name__ == "__main__":
    main()
