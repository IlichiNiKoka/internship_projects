# -*- coding: utf-8 -*-
"""人员1应用层纯单元测试：不依赖 Spark、Flask、真实 Redis 或网络。"""

from __future__ import annotations

import sys
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ai.intent.classifier import IntentClassifier
from app.ai.summary.generator import SummaryGenerator
from app.ai.summary.llm_client import MockClient
from app.application.clients import AnalysisAPIError, AnalysisClient, HTTPAnalysisClient
from app.application.memory import (
    InMemorySessionStore,
    LangChainSessionMemory,
    RedisSessionStore,
    build_session_store,
)
from app.application.models import AnalysisRecord, ConversationMessage, ConversationSession, new_id
from app.application.reports import MedicalReportService
from app.application.service import MedicalAssistantService
from app.application.tools import (
    ParameterAdapter,
    RetryPolicy,
    ToolExecutor,
    ToolInvocationError,
    ToolRegistry,
    normalize_for_summary,
)


class FakeAnalysisClient(AnalysisClient):
    mode = "fake"

    def __init__(self):
        self.calls = []

    def run_aggregation(self, params):
        self.calls.append(("aggregation", params))
        dimension = params["dimensions"][0]
        return {
            "dimensions": [{"key": dimension, "column": dimension, "label": dimension}],
            "metrics": [{"key": params["metrics"][0], "label": "住院人次", "unit": "人次"}],
            "filters": params.get("filters", []),
            "rows": [{dimension: "Female", params["metrics"][0]: 12}],
            "row_count": 1,
            "truncated": False,
        }

    def run_algorithm(self, name, params):
        self.calls.append((name, params))
        results = {
            "statistics": {
                "overview": {
                    "discharge_count": 600,
                    "avg_length_of_stay": 10.5,
                    "avg_total_charges": 3995.0,
                    "avg_total_costs": 1798.0,
                    "emergency_rate_pct": 50.0,
                },
                "distributions": {"gender": [{"value": "Female", "count": 300}]},
                "top_diseases": [{"disease": "A", "discharge_count": 100}],
            },
            "association": {
                "transaction_count": 500,
                "rules": [{
                    "antecedent": {"diagnosis": "A"},
                    "consequent": {"procedure": "B"},
                    "support": 0.2,
                    "confidence": 0.8,
                    "lift": 1.4,
                }],
            },
            "cost_prediction": {
                "mode": "predict", "predicted_total_charges": 12345.0,
                "currency": "USD", "input": params.get("sample", {}),
            },
            "readmission_risk": {
                "mode": "score", "risk_score": 65.0, "risk_level": "High",
                "contributions": [],
            },
        }
        return {
            "algorithm": name,
            "status": "success",
            "message": "ok",
            "metrics": {},
            "result": results[name],
        }

    def metadata(self, kind=None):
        self.calls.append(("metadata", {"kind": kind}))
        return {kind or "dimensions": [{"key": "age_group"}]}


def build_test_service(client=None):
    client = client or FakeAnalysisClient()
    registry = ToolRegistry.build_default(client, default_limit=20, max_limit=100)
    executor = ToolExecutor(
        registry,
        RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0),
        sleeper=lambda _: None,
    )
    generator = SummaryGenerator(MockClient())
    store = InMemorySessionStore(ttl_seconds=60, max_sessions=20)
    reports = MedicalReportService(generator, max_analyses=5)
    service = MedicalAssistantService(
        intent_classifier=IntentClassifier(),
        summary_generator=generator,
        tool_executor=executor,
        session_store=store,
        report_service=reports,
        max_messages=20,
        max_analyses=10,
    )
    return service, client, store


class ToolRegistryTests(unittest.TestCase):
    def test_all_supported_intents_have_explicit_tools(self):
        client = FakeAnalysisClient()
        registry = ToolRegistry.build_default(client)
        expected = {
            "aggregation_query", "statistics_overview", "association_analysis",
            "cost_prediction", "readmission_risk", "metadata_query",
        }
        self.assertEqual(expected, {item["intent"] for item in registry.metadata()})
        self.assertIsNone(registry.for_intent("unsupported"))

    def test_cost_prediction_parameter_conversion(self):
        adapter = ParameterAdapter()
        result = adapter.cost_prediction({
            "mode": "predict",
            "sample": {
                "length_of_stay": 5,
                "type_of_admission": "Emergency",
                "payment_typology_1": "Medicare",
                "apr_medical_surgical_description": "Medical",
                "apr_severity_of_illness_description": "Major",
                "age_group": "70 or Older",
            },
        })
        self.assertEqual(result["sample"]["admission_type"], "Emergency")
        self.assertEqual(result["sample"]["payment_type"], "Medicare")
        self.assertEqual(result["sample"]["medical_surgical"], "Medical")
        self.assertEqual(result["sample"]["severity_code"], 3)
        self.assertNotIn("type_of_admission", result["sample"])

    def test_aggregation_removes_fixed_redundant_dimension(self):
        adapter = ParameterAdapter()
        result = adapter.aggregation({
            "dimensions": ["discharge_year", "facility_name"],
            "metrics": ["avg_total_charges"],
            "filters": [{"field": "discharge_year", "op": "eq", "value": 2021}],
        })
        self.assertEqual(result["dimensions"], ["facility_name"])
        self.assertEqual(result["limit"], 100)

    def test_transient_error_retries_but_bad_request_does_not(self):
        class FlakyClient(FakeAnalysisClient):
            def __init__(self, retryable):
                super().__init__()
                self.count = 0
                self.retryable = retryable

            def metadata(self, kind=None):
                self.count += 1
                raise AnalysisAPIError(
                    "failed", status_code=503 if self.retryable else 400,
                    retryable=self.retryable,
                )

        transient = FlakyClient(True)
        executor = ToolExecutor(
            ToolRegistry.build_default(transient),
            RetryPolicy(max_attempts=3, base_delay_seconds=0),
            sleeper=lambda _: None,
        )
        with self.assertRaises(ToolInvocationError) as ctx:
            executor.execute("metadata_query", {})
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(transient.count, 3)

        bad = FlakyClient(False)
        executor = ToolExecutor(
            ToolRegistry.build_default(bad),
            RetryPolicy(max_attempts=3, base_delay_seconds=0),
            sleeper=lambda _: None,
        )
        with self.assertRaises(ToolInvocationError) as ctx:
            executor.execute("metadata_query", {})
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(bad.count, 1)

    def test_algorithm_result_is_adapted_for_summary(self):
        raw = FakeAnalysisClient().run_algorithm("statistics", {})
        result = normalize_for_summary("statistics_overview", raw)
        self.assertEqual(result["total_discharges"], 600)
        self.assertEqual(result["avg_total_charges"], 3995.0)
        self.assertEqual(result["emergency_rate"], 50.0)

    def test_first_transient_failure_then_success_records_attempts(self):
        class OnceFlaky(FakeAnalysisClient):
            def __init__(self):
                super().__init__()
                self.count = 0

            def metadata(self, kind=None):
                self.count += 1
                if self.count == 1:
                    raise AnalysisAPIError("busy", status_code=503, retryable=True)
                return {"dimensions": []}

        client = OnceFlaky()
        executor = ToolExecutor(
            ToolRegistry.build_default(client),
            RetryPolicy(max_attempts=3, base_delay_seconds=0),
            sleeper=lambda _: None,
        )
        result = executor.execute("metadata_query", {"kind": "dimensions"})
        self.assertEqual(result.attempts, 2)
        self.assertEqual(client.count, 2)

    def test_http_client_unwraps_standard_response_and_wraps_algorithm_params(self):
        captured = {}

        class Response:
            status = 200

            def read(self):
                return json.dumps({
                    "code": 200, "message": "OK", "data": {"result": 7},
                    "query_time": 0.1, "trace_id": "trace",
                }).encode()

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return Response()

        client = HTTPAnalysisClient("http://analysis.local", timeout=2, opener=opener)
        result = client.run_algorithm("statistics", {"top_n": 5})
        self.assertEqual(result, {"result": 7})
        self.assertEqual(captured["body"], {"params": {"top_n": 5}})
        self.assertTrue(captured["url"].endswith("/api/v1/algorithms/statistics/run"))


class MemoryTests(unittest.TestCase):
    def test_round_trip_isolated_copy_and_langchain_protocol(self):
        store = InMemorySessionStore(ttl_seconds=60, max_sessions=2)
        session = ConversationSession(id="session_12345678")
        session.messages.append(ConversationMessage(
            id=new_id("msg"), role="user", content="你好"
        ))
        store.save(session)
        loaded = store.load(session.id)
        loaded.messages[0].content = "changed"
        self.assertEqual(store.load(session.id).messages[0].content, "你好")

        memory = LangChainSessionMemory(store, session.id)
        history = memory.load_memory_variables({})["history"]
        self.assertEqual(len(history), 1)
        content = history[0]["content"] if isinstance(history[0], dict) else history[0].content
        self.assertEqual(content, "你好")

    def test_redis_json_round_trip_uses_ttl(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}
                self.ttls = {}

            def get(self, key):
                return self.values.get(key)

            def setex(self, key, ttl, value):
                self.values[key] = value
                self.ttls[key] = ttl

            def delete(self, key):
                return int(self.values.pop(key, None) is not None)

            def ping(self):
                return True

        fake = FakeRedis()
        store = RedisSessionStore(
            "redis://unused", ttl_seconds=120, redis_client=fake
        )
        session = ConversationSession(id="session_redis_123")
        session.messages.append(ConversationMessage(
            id=new_id("msg"), role="user", content="隐私安全测试"
        ))
        store.save(session)
        loaded = store.load(session.id)
        self.assertEqual(loaded.messages[0].content, "隐私安全测试")
        self.assertEqual(next(iter(fake.ttls.values())), 120)

    def test_production_requires_redis(self):
        settings = SimpleNamespace(
            env="production", conversation_backend="auto", redis_url="",
            conversation_ttl_seconds=60, conversation_max_sessions=5,
        )
        with self.assertRaises(RuntimeError):
            build_session_store(settings)


class ConversationTests(unittest.TestCase):
    def test_multi_turn_context_and_report_reference(self):
        service, client, _ = build_test_service()
        first = service.chat("按年龄段统计住院人次")
        self.assertEqual(first.status, "completed")
        session_id = first.session_id
        first_analysis_id = first.analysis["id"]

        second = service.chat("那按性别呢", session_id=session_id)
        self.assertEqual(second.status, "completed")
        self.assertTrue(second.context["context_inherited"])
        self.assertEqual(second.analysis["tool_input"]["dimensions"], ["gender"])

        report_result = service.chat("生成刚才的报告", session_id=session_id)
        self.assertEqual(report_result.status, "report_generated")
        self.assertIsNotNone(report_result.report)
        self.assertEqual(
            report_result.report["source_analysis_ids"],
            [second.analysis["id"]],
        )
        self.assertNotEqual(first_analysis_id, second.analysis["id"])

    def test_idempotency_prevents_duplicate_tool_call(self):
        service, client, _ = build_test_service()
        request_id = "request_12345678"
        first = service.chat("整体情况", request_id=request_id)
        call_count = len(client.calls)
        replay = service.chat(
            "整体情况", session_id=first.session_id, request_id=request_id
        )
        self.assertEqual(replay.status, "replayed")
        self.assertEqual(len(client.calls), call_count)

    def test_missing_history_for_report_requests_clarification(self):
        service, _, _ = build_test_service()
        result = service.chat("生成刚才的报告")
        self.assertEqual(result.status, "needs_clarification")
        self.assertIn("还没有", result.assistant_message["content"])

    def test_cost_prediction_uses_converted_fields(self):
        service, client, _ = build_test_service()
        result = service.chat("预测一个老年人急诊入院5天的费用")
        self.assertEqual(result.status, "completed")
        call = next(item for item in client.calls if item[0] == "cost_prediction")
        self.assertEqual(call[1]["sample"]["admission_type"], "Emergency")
        self.assertEqual(call[1]["sample"]["length_of_stay"], 5)

    def test_concurrent_messages_in_same_session_are_not_lost(self):
        service, _, _ = build_test_service()
        first = service.chat("整体情况")
        barrier = threading.Barrier(2)

        def send(_):
            barrier.wait()
            return service.chat("整体情况", session_id=first.session_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, range(2)))
        self.assertTrue(all(item.status == "completed" for item in results))
        history = service.get_session(first.session_id)
        self.assertEqual(len(history["messages"]), 6)
        self.assertEqual(len(history["analyses"]), 3)


class ReportTests(unittest.TestCase):
    def test_report_contains_stable_sections_charts_and_provenance(self):
        generator = SummaryGenerator(MockClient())
        service = MedicalReportService(generator)
        raw = FakeAnalysisClient().run_algorithm("statistics", {})
        summary_data = normalize_for_summary("statistics_overview", raw)
        record = AnalysisRecord(
            id=new_id("ana"), message_id=new_id("msg"), query="整体情况",
            intent="statistics_overview", tool_name="medical_statistics",
            tool_input={"top_n": 10},
            result={"data": raw, "summary_data": summary_data},
            summary={}, attempts=1, elapsed_seconds=0.1,
        )
        report = service.generate(session_id="session_12345678", analyses=[record])
        self.assertEqual(report["schema_version"], "1.0")
        self.assertEqual(report["source_analysis_ids"], [record.id])
        self.assertEqual(report["sections"][0]["key_metrics"][0]["value"], 600)
        self.assertTrue(report["charts"])
        self.assertEqual(report["provenance"][0]["tool"], "medical_statistics")


class AgentPathContractTests(unittest.TestCase):
    """Agent 模式（注入 AIService）的分析结果必须带 summary_data。

    回归背景：Agent 路径曾缺失该字段，导致前端图表永远为空、
    报告层误判「空数据」而将全部章节 fail-closed 判为不可信。
    """

    class _FakeAgentIntent:
        def __init__(self):
            self.intent = "freeform_query"
            self.spec = None
            self.params = {"dimensions": ["gender"], "metrics": ["discharge_count"]}

        def to_dict(self):
            return {"query": "q", "intent": self.intent, "confidence": 0.9,
                    "params": {}, "matched_signals": {}}

    class _FakeAgentSummary:
        def __init__(self, text):
            self._text = text

        def to_dict(self):
            return {"text": self._text, "llm_provider": "fake",
                    "fell_back_to_mock": False, "empty_source": False,
                    "hallucination": {"passed": True, "mode": "prompt_only"}}

    class _FakeAgentService:
        """模拟 AIService.execute：primary 为聚合原始结果（无 summary_data 键）。"""

        def __init__(self, primary_payload):
            self._primary = primary_payload

        def execute(self, query, *, history=""):
            return SimpleNamespace(
                intent=AgentPathContractTests._FakeAgentIntent(),
                analysis={
                    "agent": True,
                    "calls": [{"tool": "aggregation", "params": {},
                               "explanation": "", "result": self._primary}],
                    "primary": self._primary,
                },
                summary=AgentPathContractTests._FakeAgentSummary(
                    "按性别统计：女性出院人次为 12,300 人。"
                ),
            )

    def test_agent_chat_result_carries_summary_data_and_report_trusted(self):
        client = FakeAnalysisClient()
        registry = ToolRegistry.build_default(client, default_limit=20, max_limit=100)
        executor = ToolExecutor(
            registry,
            RetryPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0),
            sleeper=lambda _: None,
        )
        generator = SummaryGenerator(MockClient())
        store = InMemorySessionStore(ttl_seconds=60, max_sessions=10)
        reports = MedicalReportService(generator, max_analyses=5)
        agent_primary = {
            "dimensions": [{"key": "gender", "column": "gender", "label": "性别"}],
            "metrics": [{"key": "discharge_count", "label": "住院人次", "unit": "人次"}],
            "rows": [{"gender": "Female", "discharge_count": 12300}],
            "row_count": 1,
        }
        service = MedicalAssistantService(
            intent_classifier=IntentClassifier(),
            summary_generator=generator,
            tool_executor=executor,
            session_store=store,
            report_service=reports,
            ai_service=self._FakeAgentService(agent_primary),
        )

        result = service.chat("按性别统计住院人次")
        assembled = (result.analysis or {}).get("result") or {}
        summary_data = assembled.get("summary_data")
        self.assertIsInstance(summary_data, dict)
        self.assertEqual(summary_data.get("rows"), [{"gender": "Female", "discharge_count": 12300}])

        # 用该记录生成报告：摘要与数字一致性校验都应通过
        report = service.generate_report(result.session_id)
        section = report["sections"][0]
        self.assertTrue(section["summary_validation"]["trusted"])
        self.assertNotEqual(
            section["narrative"],
            "该项自动摘要未通过数据一致性校验，请以本节结构化数据为准。",
        )


class _StaticSummaryGenerator:
    """返回固定文本的摘要生成器替身（用于数字一致性校验测试）。"""

    def __init__(self, text):
        self._text = text

    def generate(self, *, user_query, intent_label, intent_key, analysis_result):
        return SimpleNamespace(to_dict=lambda: {
            "text": self._text,
            "llm_provider": "static",
            "fell_back_to_mock": False,
            "empty_source": False,
            "hallucination": {"passed": True, "mode": "prompt_only"},
        })


class ReportNumericConsistencyTests(unittest.TestCase):
    """需求：报告必须校验摘要中的数字一致性（fail-closed）。"""

    @staticmethod
    def _record(summary_data):
        return AnalysisRecord(
            id=new_id("ana"), message_id=new_id("msg"), query="按性别统计",
            intent="aggregation_query", tool_name="aggregate",
            tool_input={"dimensions": ["gender"]},
            result={"data": {}, "summary_data": summary_data},
            summary={}, attempts=1, elapsed_seconds=0.1,
        )

    def test_consistent_numbers_are_trusted(self):
        data = {"rows": [{"gender": "Female", "discharge_count": 12300}], "row_count": 1}
        service = MedicalReportService(_StaticSummaryGenerator(
            "女性出院人次为 12,300 人，共 1 个分组。"
        ))
        report = service.generate(session_id="ses_x", analyses=[self._record(data)])
        section = report["sections"][0]
        self.assertTrue(section["summary_validation"]["trusted"])
        self.assertTrue(section["summary_validation"]["hallucination"]["passed"])
        self.assertEqual(report["validation"]["all_summaries_trusted"], True)
        self.assertIn("12,300", report["executive_summary"])

    def test_fabricated_numbers_are_excluded_from_report(self):
        data = {"rows": [{"gender": "Female", "discharge_count": 12300}], "row_count": 1}
        service = MedicalReportService(_StaticSummaryGenerator(
            "女性出院人次为 45,678 人，占比 88.8%。"
        ))
        report = service.generate(session_id="ses_x", analyses=[self._record(data)])
        section = report["sections"][0]
        self.assertFalse(section["summary_validation"]["trusted"])
        self.assertFalse(section["summary_validation"]["hallucination"]["passed"])
        unmatched = section["summary_validation"]["hallucination"]["unmatched"]
        self.assertTrue(any(abs(n - 45678) < 1 for n in unmatched))
        codes = [w["code"] for w in report["warnings"]]
        self.assertIn("UNTRUSTED_SUMMARY", codes)
        self.assertFalse(report["validation"]["all_summaries_trusted"])
        # 编造数字的摘要不得进入报告结论
        self.assertNotIn("45,678", report["executive_summary"])
        self.assertNotIn("45678", report["executive_summary"])

    # ---- 2026-08-27 修复：消除合法摘要的误报（单位/舍入/占比推导）----

    def test_percent_fraction_unit_equivalence_is_trusted(self):
        """源存小数(0.146)，文本写百分号(14.6%)，视为一致。"""
        from app.ai.summary.hallucination import check as numeric_check
        report = numeric_check(
            {"mode": "profile", "high_risk_rate": 0.146,
             "level_distribution": [
                 {"level": "High", "count": 1200, "ratio": 0.146},
                 {"level": "Low", "count": 7014, "ratio": 0.854}]},
            "高风险人群比例为 14.6%，其中 70 岁及以上占比最大。",
        )
        self.assertTrue(report.passed, report.unmatched)

    def test_two_decimal_rounding_of_small_fraction_is_trusted(self):
        """源存 0.146（4 位小数），文本四舍五入为 0.15（保留 2 位）。"""
        from app.ai.summary.hallucination import check as numeric_check
        report = numeric_check(
            {"high_risk_rate": 0.146},
            "高风险人群比例约 0.15。",
        )
        self.assertTrue(report.passed, report.unmatched)

    def test_derived_percentage_of_distribution_is_trusted(self):
        """LLM 从 rows 人次推导占比（656100/合计=31.2%），应视为一致。"""
        from app.ai.summary.hallucination import check as numeric_check
        rows = [
            {"age_group": "0-17", "discharge_count": 86137},
            {"age_group": "18-29", "discharge_count": 233077},
            {"age_group": "30-49", "discharge_count": 500000},
            {"age_group": "50-69", "discharge_count": 626274},
            {"age_group": "70 or Older", "discharge_count": 656100},
        ]
        report = numeric_check(
            {"rows": rows, "row_count": 5},
            "70 岁及以上占比最高（约 31.2%），共 656,100 人次，其次 50-69 岁（29.8%）。",
        )
        self.assertTrue(report.passed, report.unmatched)

    def test_fabricated_percentage_is_still_rejected(self):
        """编造的占比（88.8%）不可由 rows 推导，仍须拦截。"""
        from app.ai.summary.hallucination import check as numeric_check
        rows = [
            {"age_group": "0-17", "discharge_count": 86137},
            {"age_group": "18-29", "discharge_count": 233077},
            {"age_group": "30-49", "discharge_count": 500000},
            {"age_group": "50-69", "discharge_count": 626274},
            {"age_group": "70 or Older", "discharge_count": 656100},
        ]
        report = numeric_check(
            {"rows": rows, "row_count": 5},
            "70 岁及以上占比最高（约 88.8%）。",
        )
        self.assertFalse(report.passed)
        self.assertTrue(any(abs(n - 88.8) < 0.01 for n in report.unmatched))

    def test_report_level_integration_with_percent_text(self):
        """报告级集成：百分比/占比文本的摘要应被纳入可信结论。"""
        data = {
            "mode": "profile", "high_risk_rate": 0.25,
            "level_distribution": [
                {"level": "High", "count": 250, "ratio": 0.25},
                {"level": "Low", "count": 750, "ratio": 0.75}],
        }
        service = MedicalReportService(_StaticSummaryGenerator(
            "高风险人群比例约 25%，低风险人群占比 75%。"
        ))
        report = service.generate(session_id="ses_x", analyses=[self._record(data)])
        section = report["sections"][0]
        self.assertTrue(section["summary_validation"]["trusted"])
        self.assertTrue(section["summary_validation"]["hallucination"]["passed"])
        self.assertTrue(report["validation"]["all_summaries_trusted"])


if __name__ == "__main__":
    unittest.main()
