# 单机伪分布式 HDFS（NameNode + DataNode 同容器）
# 基于 Apache 官方 hadoop-runner 基础镜像构建的 3.5.0 便捷镜像，
# 与项目 Windows 客户端安装的 hadoop-3.5.0 版本保持一致。
FROM apache/hadoop:3.5.0

ENV HDFS_NAMENODE_DIR=/data/namenode \
    HDFS_DATANODE_DIR=/data/datanode \
    HDFS_REPLICATION=1

COPY core-site.xml  /opt/hadoop/etc/hadoop/core-site.xml
COPY hdfs-site.xml  /opt/hadoop/etc/hadoop/hdfs-site.xml
COPY entrypoint.sh  /entrypoint.sh
# 基础镜像默认以非 root 用户 hadoop 运行，切回 root 以初始化目录
USER root
# 构建期兜底：剥离 Windows 检出可能带入的 CR，避免 "bash\r" 启动失败
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh \
 && mkdir -p /data/namenode /data/datanode

EXPOSE 8020 9870 9864 9866

ENTRYPOINT ["/entrypoint.sh"]
