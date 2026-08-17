# -*- coding: utf-8 -*-
"""统一响应封装测试（3.3.3）。"""

import json

from app.core.error_codes import ErrorCode
from app.core.response import build, error, success

REQUIRED_KEYS = {"code", "message", "data", "query_time", "trace_id"}


def test_build_structure():
    body = build(ErrorCode.OK, "OK", {"a": 1})
    assert REQUIRED_KEYS <= set(body.keys())
    assert body["code"] == 200
    assert body["message"] == "OK"
    assert body["data"] == {"a": 1}
    assert isinstance(body["query_time"], float)
    assert isinstance(body["trace_id"], str) and len(body["trace_id"]) == 16


def test_success_helper():
    resp, status = success({"rows": []}, message="OK")
    assert status == 200
    body = json.loads(resp.get_data(as_text=True))
    assert body["code"] == 200


def test_error_helper():
    resp, status = error(ErrorCode.ALGORITHM_NOT_FOUND, detail={"algorithm": "x"})
    assert status == 404
    body = json.loads(resp.get_data(as_text=True))
    assert body["code"] == 404
    assert body["data"] == {"detail": {"algorithm": "x"}}
