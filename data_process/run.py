# -*- coding: utf-8 -*-
"""开发环境启动入口：python run.py

生产环境请使用 wsgi.py + gunicorn（Linux），或 Flask 生产级部署方案。

关键：Windows 下 Spark / Hadoop 依赖 JAVA_HOME、HADOOP_HOME 环境变量，
必须在任何 import pyspark（以及任何 transitively 导入 data_provider / ai
服务层的代码）之前完成解析并注入。因此本文件在顶部先加载 settings，
再调用 utils/spark.py 的探测函数设置环境变量，最后才导入 app。
"""

from __future__ import annotations

import os
import sys

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


def main() -> None:
    settings = Settings.load()
    app = create_app(settings)
    print(f"\n  {settings.app_name} v{settings.version}")
    print(f"  * 运行环境 : {settings.env}")
    print(f"  * 数据源   : {settings.data_source}"
          f"（{settings.db_host}:{settings.db_port}/{settings.db_name}"
          f"/{settings.db_table}）")
    print(f"  * Spark    : {settings.spark_master}")
    if os.environ.get("HADOOP_HOME"):
        print(f"  * Hadoop   : {os.environ['HADOOP_HOME']}")
    print(f"  * 服务地址 : http://{settings.host}:{settings.port}")
    print(f"  * 健康检查 : http://{settings.host}:{settings.port}/api/v1/health\n")
    app.run(host=settings.host, port=settings.port, debug=settings.debug,
            threaded=True)


if __name__ == "__main__":
    main()
