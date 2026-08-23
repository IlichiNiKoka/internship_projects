# -*- coding: utf-8 -*-
"""API v1 蓝本注册。"""

from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from app.api.v1.aggregation import bp as aggregation_bp
    from app.api.v1.algorithms import bp as algorithms_bp
    from app.api.v1.ai import bp as ai_bp
    from app.api.v1.health import bp as health_bp
    from app.api.v1.meta import bp as meta_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(meta_bp)
    app.register_blueprint(aggregation_bp)
    app.register_blueprint(algorithms_bp)
    app.register_blueprint(ai_bp)
