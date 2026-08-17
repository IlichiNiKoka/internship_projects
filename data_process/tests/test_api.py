# -*- coding: utf-8 -*-
"""API 端到端测试（3.3.1 / 3.3.2 / 3.3.3）。

覆盖：健康检查、元数据、聚合接口、算法统一接口、
统一响应结构与错误码、trace_id 透传。
"""

STANDARD_KEYS = {"code", "message", "data", "query_time", "trace_id"}


def _post(client, url, payload):
    return client.post(url, json=payload)


def _body(resp):
    return resp.get_json()


# ---------------------------------------------------------------------------
# 健康检查 / 元数据
# ---------------------------------------------------------------------------
def test_health(client):
    resp = client.get("/api/v1/health")
    body = _body(resp)
    assert resp.status_code == 200
    assert body["code"] == 200
    assert body["data"]["status"] == "ok"
    assert body["data"]["data"]["row_count"] == 600


def test_meta_dimensions(client):
    body = _body(client.get("/api/v1/meta/dimensions"))
    keys = {d["key"] for d in body["data"]}
    assert {"age_group", "gender", "ccsr_diagnosis_description",
            "payment_typology_1", "discharge_year"} <= keys


def test_meta_metrics(client):
    body = _body(client.get("/api/v1/meta/metrics"))
    keys = {m["key"] for m in body["data"]}
    assert {"discharge_count", "avg_length_of_stay",
            "avg_total_charges", "avg_total_costs"} <= keys


def test_meta_algorithms(client):
    body = _body(client.get("/api/v1/meta/algorithms"))
    names = {a["name"] for a in body["data"]}
    assert {"group_aggregation", "statistics", "association",
            "cost_prediction", "readmission_risk"} <= names


def test_algorithm_meta_detail(client):
    resp = client.get("/api/v1/algorithms/statistics")
    body = _body(resp)
    assert resp.status_code == 200
    assert body["data"]["display_name"] == "统计指标计算"
    assert body["data"]["params"]


# ---------------------------------------------------------------------------
# 聚合接口
# ---------------------------------------------------------------------------
def test_aggregation_run(client):
    resp = _post(client, "/api/v1/aggregations/run", {
        "dimensions": ["age_group"],
        "metrics": ["discharge_count", "avg_length_of_stay"],
        "limit": 10,
    })
    body = _body(resp)
    assert resp.status_code == 200
    assert body["code"] == 200
    assert body["data"]["row_count"] == 5
    assert body["data"]["rows"][0]["discharge_count"] == 120


def test_aggregation_with_filters(client):
    resp = _post(client, "/api/v1/aggregations/run", {
        "dimensions": ["gender"],
        "metrics": ["discharge_count"],
        "filters": [{"field": "type_of_admission", "op": "eq", "value": "Emergency"}],
    })
    body = _body(resp)
    assert body["code"] == 200
    assert sum(r["discharge_count"] for r in body["data"]["rows"]) == 150


def test_aggregation_invalid_dimension_400(client):
    resp = _post(client, "/api/v1/aggregations/run", {
        "dimensions": ["hacked_column"],
        "metrics": ["discharge_count"],
    })
    body = _body(resp)
    assert resp.status_code == 400
    assert body["code"] == 400
    assert "detail" in body["data"]


def test_aggregation_missing_body_400(client):
    resp = client.post("/api/v1/aggregations/run", data="{}",
                       content_type="application/json")
    body = _body(resp)
    assert resp.status_code == 400
    assert body["code"] == 400


# ---------------------------------------------------------------------------
# 算法统一接口
# ---------------------------------------------------------------------------
def test_algorithm_run_statistics(client):
    resp = _post(client, "/api/v1/algorithms/statistics/run", {"params": {"top_n": 5}})
    body = _body(resp)
    assert resp.status_code == 200
    assert body["data"]["status"] == "success"
    assert body["data"]["result"]["overview"]["discharge_count"] == 600


def test_algorithm_run_not_found_404(client):
    resp = _post(client, "/api/v1/algorithms/unknown_algo/run", {"params": {}})
    body = _body(resp)
    assert resp.status_code == 404
    assert body["code"] == 404


def test_algorithm_param_error_400(client):
    """缺少必填参数 dimensions -> 参数校验失败 400。"""
    resp = _post(client, "/api/v1/algorithms/group_aggregation/run", {"params": {}})
    body = _body(resp)
    assert resp.status_code == 400
    assert body["code"] == 400


# ---------------------------------------------------------------------------
# 统一响应结构 / trace_id / 框架异常
# ---------------------------------------------------------------------------
def test_standard_response_structure(client):
    resp = client.get("/api/v1/meta/dimensions")
    body = _body(resp)
    assert STANDARD_KEYS <= set(body.keys())
    assert isinstance(body["query_time"], float)
    assert resp.headers["X-Request-ID"] == body["trace_id"]
    assert "X-Query-Time" in resp.headers


def test_trace_id_passthrough(client):
    resp = client.get("/api/v1/health", headers={"X-Request-ID": "my-trace-123"})
    body = _body(resp)
    assert body["trace_id"] == "my-trace-123"


def test_404_standardized(client):
    body = _body(client.get("/api/v1/no_such_route"))
    assert body["code"] == 404
    assert STANDARD_KEYS <= set(body.keys())


def test_405_standardized(client):
    body = _body(client.get("/api/v1/aggregations/run"))
    assert body["code"] == 405
    assert STANDARD_KEYS <= set(body.keys())
