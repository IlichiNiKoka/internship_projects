# scripts —— 数据库一键启停脚本说明

> 智慧医疗大数据与 AI 大模型分析平台 · 本地开发环境工具
> 适用范围：Windows 本地开发（后端 Spark 聚合依赖本机 MySQL + Redis）

## 目录

- `start-db.bat` —— 一键启动 MySQL + Redis（幂等：已在运行则跳过）
- `stop-db.bat` —— 一键优雅关闭 MySQL + Redis（幂等：未运行则跳过）

---

## 1. 首次使用：配置路径（必须）

两个脚本的**路径全部集中在顶部 `EDIT THESE` 区**，请改成你本机的实际路径：

```bat
rem ================= EDIT THESE =================
set "MYSQL_BASEDIR=D:\Project_env\mysql-8.0.46-winx64"        ← MySQL 解压根目录
set "MYSQL_DATADIR=D:\Project_env\mysql-8.0.46-winx64\data"  ← MySQL 数据目录
set "REDIS_HOME=D:\Project_env\Redis-8.6.3-Windows-x64-cygwin-with-Service"  ← Redis 目录
rem ==============================================
```

> ⚠️ 路径请使用**纯英文**（避免 MySQL 对中文路径的兼容性问题，无需再使用 `subst Z:` 虚拟盘符）。

---

## 2. 使用方法

### 启动数据库

双击 `start-db.bat`，输出应看到：

```
[OK]   Redis starting...
[OK]   MySQL starting...
---- Redis check ----
PONG
---- MySQL check ----
+-----------+
| VERSION() |
+-----------+
| 8.0.46    |
```

看到 `PONG` 和 MySQL 版本号即为成功（窗口关闭不影响服务，服务在后台运行）。

### 关闭数据库

双击 `stop-db.bat`，输出应看到：

```
[STOP] Redis shutting down...
[STOP] MySQL shutting down gracefully...
---- Port check (3306 / 6379) ----
[OK]   Ports 3306 and 6379 are free. All services stopped.
```

看到端口已释放即关闭成功。

---

## 3. 与项目配套的完整启停流程

```text
开始开发：
  ① 双击 scripts/start-db.bat          （MySQL + Redis）
  ② 启动后端：cd data_process && python run.py
  ③ 启动前端：cd smart-medical-frontend && npm run dev
  ④ 浏览器打开 http://localhost:5173

结束开发：
  ① 后端窗口 Ctrl+C                     （优雅关闭 Flask）
  ② 双击 scripts/stop-db.bat            （MySQL + Redis）
  ③ 前端窗口 Ctrl+C
  ④ 确认后关机
```

---

## 4. 常见问题排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动 MySQL 后 3306 一直不通 | 上次未优雅关闭，InnoDB 崩溃恢复中 | 等待 30~60 秒再检查；仍不通看 `data\*.err` 日志 |
| 关闭 Redis 报 `ERR Errors trying to SHUTDOWN` | Redis 8.6.3 (cygwin 版) RDB 保存环节 bug | 已内置 `shutdown nosave` 规避，属正常输出 |
| 后端启动报端口被占用 | 上次后端进程未退出 | `Get-Process python \| Stop-Process` 后重启 |
| 中文路径导致 mysqld 启动失败 | MySQL 对非 ASCII 路径兼容问题 | 路径改为纯英文（本项目已用 `D:\Project_env`） |
| 找不到 mysqld / redis-server | 顶部路径配置错误 | 重新核对 `EDIT THESE` 区的三个变量 |

---

## 5. 设计说明

- **ASCII-only 注释**：`.bat` 由 cmd 以 GBK 代码页解析，中文注释（UTF-8 编码）会乱码并可能误报命令错误，故全部使用英文注释。
- **优雅关闭优先**：数据库严禁用任务管理器/`taskkill /F` 强杀——MySQL 强杀会导致下次启动需 InnoDB 崩溃恢复（变慢），Redis 强杀会丢失未落盘缓存。
- **幂等设计**：脚本先检测服务是否已在运行，重复执行不会重复拉起。
- 环境要求：MySQL 8.x 免安装版 + Redis（Windows 版即可），后端 Spark JDBC 读 MySQL 还需 `mysql-connector-java` jar 放入 `<pyspark>/jars/`（详见根目录 `requirements.txt` 注释）。
