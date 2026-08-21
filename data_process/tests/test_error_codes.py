# -*- coding: utf-8 -*-
"""错误码体系测试。"""

from app.core.error_codes import ErrorCode, default_message


def test_code_ranges():
    """2xx 成功 / 4xx 客户端 / 5xx 服务端 三段体系。"""
    ok = [c for c in ErrorCode if 200 <= c < 300]
    client_err = [c for c in ErrorCode if 400 <= c < 500]
    server_err = [c for c in ErrorCode if 500 <= c < 600]
    assert ErrorCode.OK in ok
    assert len(client_err) >= 5
    assert len(server_err) >= 3


def test_default_messages_cover_all_codes():
    for code in ErrorCode:
        assert default_message(code), f"{code} 缺少默认消息"
