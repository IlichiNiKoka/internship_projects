# -*- coding: utf-8 -*-
"""数据库备份与恢复（人员2 · 二期 UC6）。

能力：
  * 手动备份：调用 mysqldump 生成带时间戳的 .sql 备份文件；
  * 手动恢复：把备份文件回灌到 MySQL（mysql < backup.sql）；
  * 自动备份：start_scheduled_backup(interval) 按固定间隔循环备份（简单定时任务，
    亦可交由系统 cron / 计划任务调度）。

安全：
  密码通过 MYSQL_PWD 环境变量传给子进程，避免出现在命令行参数（进程列表可见）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def _find_tool(name: str, bin_dir: str = "") -> str | None:
    """定位 mysqldump / mysql 可执行文件（优先 bin_dir，其次 PATH）。"""
    if bin_dir:
        for suffix in ("", ".exe"):
            candidate = Path(bin_dir) / f"{name}{suffix}"
            if candidate.exists():
                return str(candidate)
    return shutil.which(name)


def _with_mysql_pwd(password: str) -> dict:
    """把密码通过 MYSQL_PWD 传给子进程，避免出现在命令行参数。"""
    env = os.environ.copy()
    if password:
        env["MYSQL_PWD"] = password
    return env


class DatabaseBackupManager:
    """MySQL 备份 / 恢复（mysqldump + mysql 客户端）。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        database: str = "medical_analytics",
        backup_dir: Path | str = "backups",
        mysql_bin_dir: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._backup_dir = Path(backup_dir)
        self._bin_dir = mysql_bin_dir

    # ------------------------------------------------------------------
    def backup(self) -> Path:
        """生成一份带时间戳的备份文件，返回其路径。"""
        dump = _find_tool("mysqldump", self._bin_dir)
        if not dump:
            raise RuntimeError(
                "未找到 mysqldump，请确认 MySQL 已安装，或通过 mysql_bin_dir 指定其 bin 目录"
            )
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        out_path = self._backup_dir / f"{self._database}_{timestamp}.sql"

        cmd = [
            dump,
            f"--host={self._host}",
            f"--port={self._port}",
            f"--user={self._user}",
            "--single-transaction",   # 一致性快照，备份期间不锁表
            "--quick",                # 逐行导出，避免大表内存暴涨
            "--routines",
            "--triggers",
            "--databases",
            self._database,
        ]
        with open(out_path, "w", encoding="utf-8") as fh:
            proc = subprocess.run(
                cmd, stdout=fh, stderr=subprocess.PIPE, env=_with_mysql_pwd(self._password)
            )
        if proc.returncode != 0:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(f"mysqldump 失败: {proc.stderr.decode(errors='replace')}")
        return out_path

    # ------------------------------------------------------------------
    def restore(self, backup_file: Path | str) -> None:
        """把备份文件回灌到 MySQL（dump 内含 CREATE DATABASE，自动重建库表）。"""
        mysql = _find_tool("mysql", self._bin_dir)
        if not mysql:
            raise RuntimeError(
                "未找到 mysql 客户端，请确认 MySQL 已安装，或通过 mysql_bin_dir 指定其 bin 目录"
            )
        backup_file = Path(backup_file)
        if not backup_file.exists():
            raise FileNotFoundError(f"备份文件不存在: {backup_file}")

        cmd = [
            mysql,
            f"--host={self._host}",
            f"--port={self._port}",
            f"--user={self._user}",
        ]
        with open(backup_file, "r", encoding="utf-8") as fh:
            proc = subprocess.run(
                cmd, stdin=fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=_with_mysql_pwd(self._password),
            )
        if proc.returncode != 0:
            raise RuntimeError(f"恢复失败: {proc.stderr.decode(errors='replace')}")

    # ------------------------------------------------------------------
    def start_scheduled_backup(self, interval_seconds: int) -> None:
        """按固定间隔循环备份（简单定时任务，Ctrl+C 停止）。"""
        print(f"自动备份已启动：目标 {self._database}，间隔 {interval_seconds} 秒（Ctrl+C 停止）...")
        try:
            while True:
                path = self.backup()
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 备份完成: {path}")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("自动备份已停止。")
