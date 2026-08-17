# -*- coding: utf-8 -*-
"""AI 智能层测试（人员4：3.X.1 / 3.X.2 / 3.X.4）。

覆盖：
  1. 意图识别准确率 >= 90%（硬指标）
  2. 多维度查询识别（3.X.4）
  3. 模糊查询/医疗术语联想（3.X.4）
  4. 文本生成 Mock 降级（API key 留空）
  5. 幻觉检查（数字一致性）
  6. REST API 端到端
"""
from __future__ import annotations

import pytest

from app.ai.intent.catalog import INTENT_BY_KEY, intent_meta
from app.ai.intent.classifier import IntentClassifier, classify, evaluate
from app.ai.intent.terms import find_synonym, normalize_value
from app.ai.intent.training_data import TEST_SET, dataset_meta
from app.ai.summary.generator import SummaryGenerator
from app.ai.summary.hallucination import check
from app.ai.summary.llm_client import (
    DisabledClient,
    MockClient,
    build_client,
    describe_client,
)
from app.ai.service import AIService, reset_ai_service
from config.settings import testing_settings


# ===========================================================================
# 1. 意图识别准确率（硬指标 >= 90%）
# ===========================================================================
class TestIntentAccuracy:
    """需求 3.X.1：训练集 / 验证集 / 测试集准确率。"""

    def test_train_accuracy(self):
        from app.ai.intent.training_data import TRAIN_SET
        r = evaluate(TRAIN_SET)
        assert r["accuracy"] >= 0.90, f"训练集准确率 {r['accuracy']} < 0.90"

    def test_valid_accuracy(self):
        from app.ai.intent.training_data import VALID_SET
        r = evaluate(VALID_SET)
        assert r["accuracy"] >= 0.90, f"验证集准确率 {r['accuracy']} < 0.90"

    def test_test_accuracy(self):
        """硬指标：测试集准确率必须 >= 90%。"""
        r = evaluate(TEST_SET)
        assert r["accuracy"] >= 0.90, (
            f"测试集准确率 {r['accuracy']} < 0.90，错样本: {r['wrong_samples']}"
        )

    def test_dataset_size(self):
        meta = dataset_meta()
        assert meta["total"] >= 100, "数据集总量应 >= 100"
        assert "aggregation_query" in meta["intents"]
        assert "unsupported" in meta["intents"]


# ===========================================================================
# 2. 多维度 / 模糊查询 / 医疗术语联想（3.X.4）
# ===========================================================================
class TestIntentAdvanced:
    """需求 3.X.4：模糊查询、多维度查询、医疗术语联想。"""

    def setup_method(self):
        self.clf = IntentClassifier(min_confidence=0.45)

    @pytest.mark.parametrize("query,expected_dims,expected_metrics", [
        ("按年龄段和性别分组的人次", ["age_group", "gender"], ["discharge_count"]),
        ("2021年每个县的住院人次",
         ["hospital_county"], ["discharge_count"]),
        ("按疾病类型和年龄组看人次",
         ["ccsr_diagnosis_description", "age_group"], ["discharge_count"]),
    ])
    def test_multi_dimension(self, query, expected_dims, expected_metrics):
        """3.X.4 多维度：同时抽取多个维度。"""
        r = self.clf.classify(query)
        assert r.intent == "aggregation_query"
        for d in expected_dims:
            assert d in r.params.get("dimensions", []), \
                f"维度 {d} 未识别；实际 {r.params.get('dimensions')}"
        for m in expected_metrics:
            assert m in r.params.get("metrics", []), \
                f"指标 {m} 未识别；实际 {r.params.get('metrics')}"

    @pytest.mark.parametrize("query,expected_dim_value", [
        ("老年人", ("age_group", "70 or Older")),
        ("急诊入院", ("type_of_admission", "Emergency")),
        ("男性", ("gender", "Male")),
        ("极重患者", ("apr_severity_of_illness_description", "Extreme")),
        ("medicare", ("payment_typology_1", "Medicare")),
    ])
    def test_medical_term_synonym(self, query, expected_dim_value):
        """3.X.4 医疗术语联想：口语化术语 -> 标准取值。"""
        result = find_synonym(query)
        assert result is not None, f"未匹配同义词：{query}"
        assert result == expected_dim_value

    @pytest.mark.parametrize("query,expected_intent", [
        # 模糊/简短
        ("整体情况", "statistics_overview"),
        ("费用预测", "cost_prediction"),
        ("再入院风险", "readmission_risk"),
        ("算法清单", "metadata_query"),
        # 自然语序
        ("有什么算法可用", "metadata_query"),
        ("肺炎常见的操作", "association_analysis"),
        # 不支持
        ("今天天气", "unsupported"),
    ])
    def test_fuzzy_recognition(self, query, expected_intent):
        r = self.clf.classify(query)
        assert r.intent == expected_intent, \
            f"「{query}」期望 {expected_intent}，实际 {r.intent}（置信度 {r.confidence}）"

    def test_year_filter_extraction(self):
        """3.X.4 模糊查询：年份自动转 filter。"""
        r = self.clf.classify("2021年各医院的平均费用")
        assert r.intent == "aggregation_query"
        filters = r.params.get("filters", [])
        assert any(f["field"] == "discharge_year" and f["value"] == 2021
                   for f in filters)

    def test_normalize_value(self):
        assert normalize_value("age_group", "老年人") == "70 or Older"
        assert normalize_value("gender", "男") == "Male"
        assert normalize_value("age_group", "70 or Older") == "70 or Older"
        # 未在词典的取值原样返回
        assert normalize_value("race", "Asian") == "Asian"


# ===========================================================================
# 3. 文本生成（Mock 降级 + 幻觉检查）
# ===========================================================================
class TestSummaryGenerator:
    """需求 3.X.2 文本生成。"""

    def setup_method(self):
        self.gen = SummaryGenerator(MockClient(), tolerance=0.02)

    def test_empty_source(self):
        """空数据特判。"""
        r = self.gen.generate("查询", "多维度聚合查询", "aggregation_query", {})
        assert r.empty_source is True
        assert "当前没有可用的分析数据" in r.text

    def test_mock_fallback(self):
        """Mock 模式：API key 留空时降级到模板渲染。"""
        data = {"rows": [["0 to 17", 12345], ["18 to 29", 6789]],
                "dimensions": ["age_group"]}
        r = self.gen.generate("按年龄段统计住院人次", "多维度聚合查询",
                              "aggregation_query", data)
        assert r.fell_back_to_mock is True
        assert r.llm_provider == "mock"
        # 关键数字来自源数据
        assert "2" in r.text  # 共 2 条分组
        assert "12345" in r.text  # 最大分组的指标值

    def test_hallucination_pass(self):
        """幻觉检查：所有数字与源一致 -> 通过。"""
        source = {"total_discharges": 1000, "avg_charges": 5000}
        text = "平台共收录 1000 例住院记录，平均费用 5000 元。"
        report = check(source, text, tolerance=0.02)
        assert report.passed is True
        assert report.unmatched == []

    def test_hallucination_fail(self):
        """幻觉检查：生成文本中编造数字 -> 不通过。"""
        source = {"total_discharges": 1000}
        text = "平台共收录 9999 例住院记录。"
        report = check(source, text, tolerance=0.02)
        assert report.passed is False
        assert 9999 in report.unmatched

    def test_disabled_client(self):
        """provider=disabled 时返回禁用提示。"""
        gen = SummaryGenerator(DisabledClient())
        r = gen.generate("查询", "多维度聚合查询", "aggregation_query",
                         {"total_discharges": 1000})
        # DisabledClient 文本不空，但生成器直接用它返回的内容
        assert r.text  # 有输出

    def test_client_factory_auto_to_mock(self, tmp_path):
        """auto + api_key 留空 -> 自动降级 MockClient。"""
        s = testing_settings(tmp_path)
        s.llm_provider = "auto"
        s.llm_api_key = ""
        client = build_client(s)
        assert client.provider == "mock"

    def test_client_factory_deepseek_with_key(self, tmp_path):
        """provider=deepseek + 有 api_key -> OpenAICompatibleClient。"""
        s = testing_settings(tmp_path)
        s.llm_provider = "deepseek"
        s.llm_api_key = "sk-test"
        s.llm_model = "deepseek-chat"
        client = build_client(s)
        assert client.provider == "deepseek"
        summary = describe_client(client, s)
        assert summary["api_key_configured"] is True
        assert summary["base_url"] == "https://api.deepseek.com/v1"


# ===========================================================================
# 4. AI 服务编排
# ===========================================================================
class TestAIService:

    def test_recognize_intent_returns_result(self, tmp_path):
        s = testing_settings(tmp_path)
        svc = AIService(settings=s)
        r = svc.recognize_intent("2021年各医院的平均费用")
        assert r.intent == "aggregation_query"
        assert "facility_name" in r.params["dimensions"]
        assert r.confidence >= 0.45

    def test_meta_returns_capabilities(self, tmp_path):
        s = testing_settings(tmp_path)
        svc = AIService(settings=s)
        meta = svc.meta()
        assert "intents" in meta
        assert "dataset" in meta
        assert "llm" in meta
        assert len(meta["intents"]) >= 7
        assert meta["llm"]["provider"] == "mock"  # api_key 留空 -> mock


# ===========================================================================
# 5. REST API 端到端
# 注意：依赖 spark fixture（PySpark + JDK 17）。JDK 25 / sandbox 环境下
# Spark 启动会失败，是环境限制，不是 AI 代码问题。
# ===========================================================================
pytestmark = [pytest.mark.spark, pytest.mark.usefixtures("app")]


class TestAIAPI:
    """通过 Flask 测试客户端验证 API。"""

    def test_health(self, client):
        r = client.get("/api/v1/ai/health")
        assert r.status_code == 200
        data = r.get_json()
        # success() 包装一层：{code, message, data}
        assert data["code"] == 200
        assert data["data"]["status"] == "ok"
        assert data["data"]["llm_provider"] == "mock"

    def test_meta(self, client):
        r = client.get("/api/v1/ai/meta")
        assert r.status_code == 200
        data = r.get_json()["data"]
        assert "intents" in data
        assert "dataset" in data

    def test_intent_recognition(self, client):
        r = client.post("/api/v1/ai/intent", json={"query": "按年龄段统计住院人次"})
        assert r.status_code == 200
        data = r.get_json()["data"]
        assert data["intent"] == "aggregation_query"
        assert "age_group" in data["params"]["dimensions"]

    def test_intent_missing_query(self, client):
        r = client.post("/api/v1/ai/intent", json={})
        assert r.status_code == 400

    def test_summary(self, client):
        payload = {
            "query": "按年龄段统计住院人次",
            "intent_key": "aggregation_query",
            "intent_label": "多维度聚合查询",
            "analysis_result": {
                "rows": [["0 to 17", 12345]],
                "dimensions": ["age_group"],
            },
        }
        r = client.post("/api/v1/ai/summary", json=payload)
        assert r.status_code == 200
        data = r.get_json()["data"]
        assert "text" in data
        assert data["llm_provider"] == "mock"

    def test_execute(self, client):
        """端到端：意图识别 -> 下游调用 -> 文本生成。"""
        r = client.post("/api/v1/ai/execute", json={"query": "整体情况"})
        assert r.status_code == 200
        data = r.get_json()["data"]
        # 整体情况 -> statistics_overview
        assert data["intent"]["intent"] == "statistics_overview"
        # 下游 algorithm service 应被调用，statistics 算法可用
        assert "analysis" in data
        assert "summary" in data

    def test_execute_unsupported(self, client):
        r = client.post("/api/v1/ai/execute", json={"query": "今天天气"})
        assert r.status_code == 200
        data = r.get_json()["data"]
        assert data["intent"]["intent"] == "unsupported"
