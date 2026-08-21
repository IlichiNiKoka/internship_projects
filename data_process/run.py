# -*- coding: utf-8 -*-
"""开发环境启动入口：python run.py

生产环境请使用 wsgi.py + gunicorn（Linux），或 Flask 生产级部署方案。
"""

from __future__ import annotations

import sys

# Windows 控制台中文兼容
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app import create_app
from config.settings import Settings


def main() -> None:
    settings = Settings.load()
    app = create_app(settings)
    print(f"\n  {settings.app_name} v{settings.version}")
    print(f"  * 运行环境 : {settings.env}")
    print(f"  * 数据文件 : {settings.data_csv_path}")
    print(f"  * Spark    : {settings.spark_master}")
    print(f"  * 服务地址 : http://{settings.host}:{settings.port}")
    print(f"  * 健康检查 : http://{settings.host}:{settings.port}/api/v1/health\n")
    app.run(host=settings.host, port=settings.port, debug=settings.debug,
            threaded=True)


if __name__ == "__main__":
    main()
