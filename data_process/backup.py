# -*- coding: utf-8 -*-
"""数据库备份 / 恢复入口（人员2 · 二期 UC6）。

用法（在本目录 data_process/ 下执行）：
    python backup.py                      # 立即执行一次手动备份
    python backup.py --restore backups/medical_analytics_xxx.sql   # 恢复
    python backup.py --schedule --interval 86400                   # 定时自动备份（秒）

配置来自 config/settings.py 的 db_* 字段（.env / ANALYTICS_* 环境变量可覆盖）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings
from storage.backup import DatabaseBackupManager


def main() -> None:
    parser = argparse.ArgumentParser(description="MySQL 备份 / 恢复（UC6）")
    parser.add_argument("--backup", action="store_true", help="立即执行一次备份（默认行为）")
    parser.add_argument("--restore", type=Path, default=None, help="从指定备份文件恢复")
    parser.add_argument("--schedule", action="store_true", help="启动定时自动备份")
    parser.add_argument("--interval", type=int, default=86400, help="自动备份间隔秒数（默认 86400=1 天）")
    parser.add_argument("--bin-dir", type=str, default="", help="MySQL bin 目录（含 mysqldump/mysql）")
    args = parser.parse_args()

    settings = Settings.load()
    mgr = DatabaseBackupManager(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        backup_dir=settings.db_backup_dir,
        mysql_bin_dir=args.bin_dir or settings.mysql_bin_dir,
    )

    if args.restore:
        mgr.restore(args.restore)
        print(f"恢复完成: {args.restore}")
        return
    if args.schedule:
        mgr.start_scheduled_backup(args.interval)
        return
    # 默认：执行一次手动备份
    path = mgr.backup()
    print(f"备份完成: {path}")


if __name__ == "__main__":
    main()
