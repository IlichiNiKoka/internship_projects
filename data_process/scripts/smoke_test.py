# -*- coding: utf-8 -*-
"""真实数据冒烟测试（可选）：对清洗后 210 万行数据做一次端到端验证。

用法（在 data_process 目录）：
    python scripts/smoke_test.py

流程：加载数据 -> 健康检查 -> 聚合 -> 统计 -> 关联分析，全部走服务层代码，
与 REST API 完全同一实现（API 用 Flask 测试客户端调用）。
注意：首次加载约 1~2 分钟（读取并缓存 210 万行），请耐心等待。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# JAVA_HOME 由 app/utils/spark.py 自动探测（环境变量 / 候选路径），
# 如需指定，请在 .env 设置 ANALYTICS_SPARK_JAVA_HOME，无需在此写死。

from app import create_app
from config.settings import Settings


def _check(name: str, ok: bool, extra: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {extra}")


def main() -> int:
    settings = Settings.load()
    print(f"服务: {settings.app_name} v{settings.version}")
    print(f"数据: {settings.data_csv_path}")
    print(f"Spark: {settings.spark_master}")

    app = create_app(settings)
    client = app.test_client()

    # 1) 健康检查（触发数据加载）
    print("\n== 1. 健康检查（首次调用会加载并缓存全量数据）==")
    resp = client.get("/api/v1/health")
    body = resp.get_json()
    _check("health", resp.status_code == 200 and body["data"]["status"] == "ok",
           f"rows={body['data']['data'].get('row_count')}")
    if resp.status_code != 200:
        print(body)
        return 1

    # 2) 多维度聚合：年龄组 × 性别
    print("\n== 2. 多维度聚合：age_group × gender ==")
    resp = client.post("/api/v1/aggregations/run", json={
        "dimensions": ["age_group", "gender"],
        "metrics": ["discharge_count", "avg_length_of_stay", "avg_total_charges"],
        "sort": [{"field": "discharge_count", "order": "desc"}],
        "limit": 10,
    })
    body = resp.get_json()
    _check("aggregation", resp.status_code == 200 and body["data"]["row_count"] > 0,
           f"rows={body['data']['row_count']}, query_time={body['query_time']}s")
    if body["data"]["rows"]:
        print("  示例行:", body["data"]["rows"][0])

    # 3) 带过滤的聚合
    print("\n== 3. 过滤聚合：急诊入院 + 费用>50000，按疾病 Top5 ==")
    resp = client.post("/api/v1/aggregations/run", json={
        "dimensions": ["ccsr_diagnosis_description"],
        "metrics": ["discharge_count", "avg_total_charges"],
        "filters": [
            {"field": "type_of_admission", "op": "eq", "value": "Emergency"},
            {"field": "total_charges", "op": "gte", "value": 50000},
        ],
        "limit": 5,
    })
    body = resp.get_json()
    _check("filtered aggregation", body["code"] == 200)
    for r in body["data"]["rows"]:
        print(f"  {r['ccsr_diagnosis_description']}: 人次={r['discharge_count']}, "
              f"平均费用={r['avg_total_charges']}")

    # 4) 统计指标算法
    print("\n== 4. 统计指标算法 ==")
    resp = client.post("/api/v1/algorithms/statistics/run", json={"params": {"top_n": 5}})
    body = resp.get_json()
    overview = body["data"]["result"]["overview"]
    _check("statistics", body["data"]["status"] == "success",
           f"总人次={overview['discharge_count']}, 平均住院={overview['avg_length_of_stay']}天, "
           f"平均费用={overview['avg_total_charges']}")

    # 5) 关联分析算法
    print("\n== 5. 诊断-操作关联分析 ==")
    resp = client.post("/api/v1/algorithms/association/run", json={"params": {
        "antecedent": "ccsr_diagnosis_description",
        "consequent": "ccsr_procedure_description",
        "min_support": 0.005, "top_n": 5,
    }})
    body = resp.get_json()
    rules = body["data"]["result"]["rules"]
    _check("association", body["data"]["status"] == "success", f"规则数={len(rules)}")
    for r in rules[:5]:
        print(f"  {r['antecedent']['ccsr_diagnosis_description']} -> "
              f"{r['consequent']['ccsr_procedure_description']} "
              f"(支持度={r['support']}, 置信度={r['confidence']}, 提升度={r['lift']})")

    # 6) 再入院风险画像
    print("\n== 6. 再入院风险画像 ==")
    resp = client.post("/api/v1/algorithms/readmission_risk/run", json={"params": {"mode": "profile"}})
    body = resp.get_json()
    levels = body["data"]["result"]["level_distribution"]
    _check("readmission_risk", body["data"]["status"] == "success",
           f"等级分布={[(l['level'], l['count']) for l in levels]}")

    print("\n冒烟测试完成 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
