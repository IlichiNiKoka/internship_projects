# Hadoop 集群远程连接 · 验证与对接指南

> 场景：组员在 VM + CentOS 上已配置好 Hadoop 集群（HDFS），
> 我方 Windows 客户端通过 Spark 远程连接该 HDFS，将项目数据源切换为 hdfs。
> 本文件包含：① 对方（服务器端）自测命令；② 我方（客户端）验证清单。

---

## 一、连接信息登记表（让对方填写）

| 项目 | 值 |
|---|---|
| 对方电脑 IP（VM 桥接网卡 IP） | `________` |
| NameNode RPC 端口 | 默认 `8020` |
| NameNode Web / WebHDFS 端口 | 默认 `9870` |
| DataNode 端口 | 默认 `9864` |
| HDFS 数据路径 | 如 `/data/Hospital_..._clean.csv` |
| Hadoop 版本 / JDK 版本 | `________` |
| 网络模式（必须桥接） | `________` |

> ⚠️ 对方 VM 网卡必须是**桥接模式**（与你在同一局域网段），NAT 模式下你无法直连。

---

## 二、对方（CentOS 服务器端）自测命令

请在对方电脑终端按顺序执行，把输出发回：

```bash
# 1. 进程检查：应看到 NameNode、DataNode
jps

# 2. 集群状态：Live datanodes 应 ≥1，无异常
hdfs dfsadmin -report

# 3. 数据确认：/data 下应有清洗后 CSV
hdfs dfs -ls /data

# 4. 端口监听确认：8020 / 9870 / 9864 三个端口在 LISTEN
ss -tlnp | grep -E ':8020|:9870|:9864'

# 5. 本机 WebHDFS 自测：应返回 JSON 文件列表
curl "http://localhost:9870/webhdfs/v1/?op=LISTSTATUS"

# 6. 防火墙状态确认（应看到放行记录，或已禁用）
sudo firewall-cmd --list-ports

# 7. SELinux 状态（应为 Disabled 或 Permissive）
getenforce
```

**预期结果速查**

| 命令 | 通过标准 |
|---|---|
| `jps` | 出现 `NameNode`、`DataNode` 两个进程 |
| `dfsadmin -report` | `Live datanodes (1)` 且 `Remaining` 有空间 |
| `hdfs dfs -ls /data` | 显示 CSV 文件，约 817MB |
| `ss -tlnp` | 8020 / 9870 / 9864 均 LISTEN |
| WebHDFS curl | 返回 `{"FileStatuses":...}` JSON |
| `getenforce` | `Disabled` 或 `Permissive` |

---

## 三、我方（Windows 客户端）验证清单

### 1. 网络连通性

```powershell
# 填入对方 IP 后执行
$hadoopIP = "对方IP"

# ① ping
ping $hadoopIP

# ② 三个关键端口
Test-NetConnection $hadoopIP -Port 8020
Test-NetConnection $hadoopIP -Port 9870
Test-NetConnection $hadoopIP -Port 9864

# ③ WebHDFS REST 读目录（无需安装 Hadoop）
curl.exe "http://${hadoopIP}:9870/webhdfs/v1/data/?op=LISTSTATUS"
```

### 2. 修改本地配置（.env）

```ini
# 数据源切到远程 HDFS
ANALYTICS_DATA_SOURCE=hdfs
ANALYTICS_HDFS_NAMENODE=hdfs://<对方IP>:8020
ANALYTICS_HDFS_PATH=/data/Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012_clean.csv
```

### 3. 重启后端并验证

```powershell
# 停掉旧后端后重启
python run.py

# ① health：data_source 应为 hdfs://<对方IP>:8020
Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/v1/health'

# ② 聚合查询：应正常返回真实数据（首次加载 817MB 走网络，可能较慢）
$body = '{"dimensions":["age_group"],"metrics":["discharge_count"],"limit":5}'
Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/v1/aggregations/run' `
  -Method Post -ContentType 'application/json' -Body $body
```

### 4. 前端验证

浏览器打开 http://localhost:5173 → 大屏页筛选 → 数据来自远程 HDFS。

---

## 四、常见问题快速排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| ping 不通 | VM 是 NAT 模式 / 防火墙 | 对方改**桥接模式**；放行 ICMP |
| 8020 不通但 9870 通 | 防火墙只放了 Web 端口 | 对方放行 `8020`（RPC 必需） |
| 9870 打不开 | NameNode Web 未起 / 防火墙 | 对方 `jps` 确认 NameNode；放行 9870 |
| 读数据权限错误 | SELinux / HDFS 文件权限 | 对方 `setenforce 0`；`hdfs dfs -chmod -R 755 /data` |
| Spark 报 winutils 警告 | Windows 无 Hadoop 客户端 | 一般不影响；必要时设 `HADOOP_HOME` + winutils.exe |
| 首次加载很慢 | 817MB 走网络 | 正常；保留 Spark 4g 内存与分区配置 |

---

## 五、备注

- 伪分布式（单节点）对外即完整 HDFS 服务，足以满足本项目数据底座需求；
- HDFS 仅解决数据源，AI 对话相关问题（assistant 蓝本未注册等）与本数据源无关，需另行修复；
- 验证通过后如需回到 MySQL 数据源，将 `.env` 的 `ANALYTICS_DATA_SOURCE` 改回 `mysql` 并重启即可。
