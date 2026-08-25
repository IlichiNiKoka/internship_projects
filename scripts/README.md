# scripts —— 数据库一键启停脚本说明

> 智慧医疗大数据与 AI 大模型分析平台 · 本地开发环境工具
>
> **2026-08-25 起全部基础设施已容器化**：MySQL / HDFS / Redis 三个服务统一由
> Docker Compose 管理（编排文件：[deploy/docker-compose.yml](../deploy/docker-compose.yml)）。
> 本目录的 `start-db.bat` / `stop-db.bat` 只是根目录脚本的薄封装。

## 目录

- [快速开始（推荐）](#快速开始推荐)
- `start-db.bat` —— 一键启动 MySQL + HDFS + Redis（转发到根目录 `deploy-infra.bat`）
- `stop-db.bat` —— 一键停止全套容器（转发到根目录 `stop-infra.bat`）
- 根目录 `deploy-infra.bat` —— 完整部署脚本（首次自动建库导数据，幂等可重复执行）

---

## 快速开始（推荐）

**新队友 / 新机器只需三步：**

1. 安装并启动 [Docker Desktop](https://www.docker.com/products/docker-desktop/)；
2. 把 MySQL 数据文件 `sparcs_discharge_2021.ibd` 放到**项目根目录**
   （该文件约 1.5 GB，不入 Git；找人员2拷贝）；
3. 双击根目录 **`deploy-infra.bat`**，等待出现 Deployment summary 即部署完成。

脚本会自动完成：

- 拉起 `medical-mysql`（MySQL 8.0）并在首次运行时建库建表 + 导入 `.ibd` 表空间；
- 构建/拉起 `medical-hdfs`（Hadoop 3.5.0 单机伪分布式 NameNode+DataNode），
  并把本地 Parquet 快照上传到 `/data/sparcs_snapshot.parquet`（如存在）；
- 拉起 `medical-redis`（Redis 7）；
- 全程幂等：已完成过的步骤自动跳过，可随时重跑修复。

之后日常开发直接 `cd data_process && python run.py` 即可——
后端启动时会通过 compose 自动拉起未运行的 MySQL/HDFS 容器（见 `.env`
的 `ANALYTICS_MYSQL_AUTOSTART` / `ANALYTICS_HDFS_AUTOSTART`）。

### 服务地址

| 服务 | 地址 | 说明 |
|---|---|---|
| MySQL | `127.0.0.1:3306` | 库 `sparcs_discharge_2021`，root 密码同原生时期 |
| HDFS RPC | `hdfs://127.0.0.1:8020` | Spark 读数据用 |
| HDFS Web UI | http://localhost:9870 | 浏览器查看文件与节点状态 |
| Redis | `127.0.0.1:6379` | 结果缓存 |

### 常用运维命令（在 deploy/ 目录下执行）

```bash
docker compose ps          # 查看状态
docker compose logs -f medical-hdfs
docker compose down        # 停止全部（数据卷保留）
docker compose down -v     # ⚠️ 停止并删除数据卷（MySQL/HDFS 数据清空，需重跑 deploy-infra.bat 重导）
```

---

## 历史方案（已废弃）

早期版本要求本机安装免安装版 MySQL/Redis 并配置 `EDIT THESE` 路径区，
现已全部由 Docker 取代，无需任何本地数据库安装。若你的机器上仍残留
原生 `MySQL80` Windows 服务，建议停用避免抢占 3306 端口：
管理员权限运行 `sc config MySQL80 start= demand && net stop MySQL80`。

---

## 设计说明

- **ASCII-only 注释**：`.bat` 由 cmd 以 GBK 代码页解析，中文注释会乱码，故英文注释。
- **幂等设计**：先检测再执行，重复运行不会重复导数据/传文件。
- **数据不进 Git**：`.ibd`、mysqldump、Parquet 快照均被 `.gitignore` 忽略，
  队友间通过网盘/IM 同步数据文件。
- 后端 `run.py` 对 Redis 的行为仍是「随服务退出而停止」（缓存可丢弃）；
  MySQL/HDFS 则常驻（持久化数据），由 Docker `restart: unless-stopped` 策略守护。
