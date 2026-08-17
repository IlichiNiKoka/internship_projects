# -*- coding: utf-8 -*-
"""生产环境 WSGI 入口（Linux + gunicorn）：

    gunicorn -w 4 -b 0.0.0.0:5000 wsgi:app --timeout 120

注意：Spark 会话为进程级单例，worker 数建议 1~2，避免本地资源争抢；
集群模式（yarn/spark://）可适当增加。
"""

from __future__ import annotations

from app import create_app
from config.settings import Settings

settings = Settings.load()
app = create_app(settings)
