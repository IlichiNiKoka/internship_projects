#!/usr/bin/env bash
# =============================================================
# HDFS 单机伪分布式启动脚本：
#   1. NameNode 目录为空时自动格式化（数据卷持久化后不再重复）；
#   2. 后台拉起 DataNode，前台运行 NameNode；
#   3. 任一进程退出则整体退出，交由 docker restart 策略兜底。
# =============================================================
set -e

echo "[hdfs-entrypoint] NameNode dir : ${HDFS_NAMENODE_DIR}"
echo "[hdfs-entrypoint] DataNode dir : ${HDFS_DATANODE_DIR}"

if [ ! -f "${HDFS_NAMENODE_DIR}/current/VERSION" ]; then
  echo "[hdfs-entrypoint] Formatting NameNode ..."
  hdfs namenode -format -force -nonInteractive
fi

echo "[hdfs-entrypoint] Starting DataNode + NameNode ..."
hdfs datanode  &
DATANODE_PID=$!
hdfs namenode  &
NAMENODE_PID=$!

cleanup() {
  echo "[hdfs-entrypoint] Stopping daemons ..."
  kill "${DATANODE_PID}" "${NAMENODE_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 任一守护进程退出即终止容器（restart: unless-stopped 兜底重启）
wait -n "${DATANODE_PID}" "${NAMENODE_PID}"
echo "[hdfs-entrypoint] A daemon exited unexpectedly."
exit 1
