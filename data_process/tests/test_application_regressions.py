# -*- coding: utf-8 -*-
"""人员1应用编排缺陷回归测试（不依赖 Spark、真实 Redis 或网络）。"""

from __future__ import annotations

import io
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from flask import Flask

TEST_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TEST_DIR.parent
for candidate in (str(TEST_DIR), str(PROJECT_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from test_application import FakeAnalysisClient, build_test_service

from app.ai.summary.generator import SummaryGenerator
from app.ai.summary.llm_client import MockClient
from app.application.clients import AnalysisAPIError, HTTPAnalysisClient
from app.api.v1 import application as application_api
from app.api.v1.application import bp as assistant_bp
from app.application.memory import (
    InMemorySessionStore,
    LangChainSessionMemory,
    RedisSessionStore,
    SessionConflictError,
    SessionLockLostError,
    SessionLockTimeoutError,
)
from app.application.models import AnalysisRecord, new_id
from app.application.reports import MedicalReportService
from app.application.tools import (
    ParameterAdapter,
    ToolParameterError,
    ToolRegistry,
    normalize_for_summary,
)
from app.core.exceptions import (
    ConflictError,
    ComputationError,
    ParamValidationError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from app.core.error_codes import ErrorCode, default_message
from app.core.middleware import register_middlewares
from app.extensions import ext


class SummaryContractRegressionTests(unittest.TestCase):
    def test_cost_summary_only_contains_fields_produced_by_current_mode(self):
        predicted = normalize_for_summary("cost_prediction", {
            "algorithm": "cost_prediction",
            "result": {
                "mode": "predict",
                "predicted_total_charges": 12345.0,
                "currency": "USD",
            },
        })
        self.assertEqual(predicted["predicted_charge"], 12345.0)
        self.assertNotIn("mae", predicted)
        self.assertNotIn("rmse", predicted)
        self.assertNotIn("r2", predicted)

        trained = normalize_for_summary("cost_prediction", {
            "algorithm": "cost_prediction",
            "result": {
                "mode": "train",
                "metrics": {"mae": 10.0, "rmse": 12.0, "r2": 0.8},
            },
        })
        self.assertEqual(trained["r2"], 0.8)
        self.assertNotIn("predicted_charge", trained)

    def test_readmission_score_is_not_presented_as_probability_or_profile(self):
        scored = normalize_for_summary("readmission_risk", {
            "algorithm": "readmission_risk",
            "result": {"mode": "score", "risk_score": 65.0, "risk_level": "High"},
        })
        self.assertEqual(scored["risk_score"], 65.0)
        self.assertNotIn("predicted_risk", scored)
        self.assertNotIn("high_risk_rate", scored)

        profiled = normalize_for_summary("readmission_risk", {
            "algorithm": "readmission_risk",
            "result": {
                "mode": "profile",
                "level_distribution": [{"level": "High", "ratio": 0.25}],
                "high_risk_age_groups": [{"age_group": "70 or Older"}],
            },
        })
        self.assertEqual(profiled["high_risk_rate"], 0.25)
        self.assertNotIn("risk_score", profiled)

    def test_mock_summary_uses_mode_specific_medical_wording(self):
        generator = SummaryGenerator(MockClient())
        cost = generator.generate(
            user_query="预测费用",
            intent_label="住院费用预测",
            intent_key="cost_prediction",
            analysis_result={
                "mode": "predict", "predicted_charge": 12345.0, "currency": "USD"
            },
        ).to_dict()
        self.assertIn("12345", cost["text"])
        self.assertNotIn("MAE", cost["text"])
        self.assertTrue(cost["hallucination"]["passed"])

        risk = generator.generate(
            user_query="评估风险",
            intent_label="再入院风险评估",
            intent_key="readmission_risk",
            analysis_result={"mode": "score", "risk_score": 65.0, "risk_level": "High"},
        ).to_dict()
        self.assertIn("风险评分", risk["text"])
        self.assertNotIn("预测再入院概率", risk["text"])
        self.assertTrue(risk["hallucination"]["passed"])

        no_rules = generator.generate(
            user_query="挖掘关联",
            intent_label="疾病关联分析",
            intent_key="association_analysis",
            analysis_result={"rules": [], "rule_count": 0},
        ).to_dict()
        self.assertIn("未发现", no_rules["text"])
        self.assertNotIn("支持度 0", no_rules["text"])
        self.assertTrue(no_rules["hallucination"]["passed"])

    def test_report_summary_failure_is_fail_closed(self):
        class BrokenGenerator:
            def generate(self, **_kwargs):
                raise RuntimeError("summary unavailable")

        record = AnalysisRecord(
            id=new_id("ana"), message_id=new_id("msg"), query="整体情况",
            intent="statistics_overview", tool_name="medical_statistics",
            tool_input={},
            result={"summary_data": {"total_discharges": 10}},
            summary={},
        )
        report = MedicalReportService(BrokenGenerator()).generate(
            session_id=new_id("ses"), analyses=[record]
        )
        self.assertFalse(report["sections"][0]["summary_validation"]["trusted"])
        self.assertFalse(report["validation"]["all_summaries_trusted"])
        self.assertTrue(any(item["code"] == "UNTRUSTED_SUMMARY" for item in report["warnings"]))

    def test_saved_summary_is_reused_when_generator_fails(self):
        """幻觉后置校验已移除（改用提示词约束）：生成器故障时复用已保存的
        可信摘要（SAVED_SUMMARY_REUSED），而不是将整节判为不可信。"""
        class BrokenGenerator:
            def generate(self, **_kwargs):
                raise RuntimeError("summary unavailable")

        record = AnalysisRecord(
            id=new_id("ana"),
            message_id=new_id("msg"),
            query="整体情况",
            intent="statistics_overview",
            tool_name="medical_statistics",
            tool_input={},
            result={"summary_data": {"total_discharges": 10}},
            # 注意：数字必须与源数据一致（报告层会做数字一致性校验）
            summary={
                "text": "平台共有 10 条记录。",
                "llm_provider": "mock",
                "empty_source": False,
                "hallucination": {"passed": True},
            },
        )
        report = MedicalReportService(BrokenGenerator()).generate(
            session_id=new_id("ses"), analyses=[record]
        )
        section = report["sections"][0]
        self.assertTrue(section["summary_validation"]["trusted"])
        self.assertIn("10", section["narrative"])
        self.assertTrue(
            any(item["code"] == "SAVED_SUMMARY_REUSED" for item in report["warnings"])
        )


class ClientAndParameterRegressionTests(unittest.TestCase):
    def test_aliased_business_codes_keep_generic_http_defaults(self):
        self.assertEqual(default_message(ErrorCode.BAD_REQUEST), "请求格式错误")
        self.assertEqual(default_message(ErrorCode.NOT_FOUND), "资源不存在")
        self.assertEqual(default_message(ErrorCode.INTERNAL_ERROR), "服务内部错误")
        self.assertEqual(ParamValidationError().message, "参数校验失败")
        self.assertEqual(ComputationError().message, "大数据计算任务执行失败")
        self.assertEqual(int(ErrorCode.PAYLOAD_TOO_LARGE), 413)

    def test_non_json_503_remains_retryable(self):
        def opener(request, timeout):
            raise HTTPError(
                request.full_url, 503, "busy", hdrs=None,
                fp=io.BytesIO(b"<html>temporarily unavailable</html>"),
            )

        client = HTTPAnalysisClient("https://analysis.invalid", opener=opener)
        with self.assertRaises(AnalysisAPIError) as ctx:
            client.metadata("dimensions")
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertTrue(ctx.exception.retryable)

    def test_bad_numeric_parameters_are_business_parameter_errors(self):
        adapter = ParameterAdapter()
        with self.assertRaises(ToolParameterError):
            adapter.association({"min_support": "not-a-number"})
        with self.assertRaises(ToolParameterError):
            adapter.cost_prediction({"mode": "train", "train_ratio": 2})
        with self.assertRaises(ToolParameterError):
            adapter.cost_prediction({
                "mode": "predict", "sample": {"length_of_stay": 0}
            })
        with self.assertRaises(ToolParameterError):
            adapter.aggregation({"dimensions": "age_group"})

    def test_readmission_adapter_drops_unknown_or_sensitive_sample_fields(self):
        normalized = ParameterAdapter().readmission_risk({
            "mode": "score",
            "sample": {
                "age_group": "70 or Older",
                "length_of_stay": 12,
                "patient_name": "should-not-be-stored",
            },
        })
        self.assertNotIn("patient_name", normalized["sample"])
        self.assertEqual(normalized["sample"]["length_of_stay"], 12.0)

    def test_langchain_structured_tool_invokes_registered_executor(self):
        client = FakeAnalysisClient()
        registry = ToolRegistry.build_default(client)
        tools = registry.as_langchain_tools()
        if not tools:
            self.skipTest("langchain-core 未安装")
        metadata_tool = next(item for item in tools if item.name == "medical_metadata")
        result = metadata_tool.invoke({"params": {"kind": "dimensions"}})
        self.assertIn("dimensions", result)


class MemoryConcurrencyRegressionTests(unittest.TestCase):
    class _FakeRedis:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def setex(self, key, _ttl, value):
            self.values[key] = value

        def delete(self, key):
            return int(self.values.pop(key, None) is not None)

        def ping(self):
            return True

    def test_stale_redis_snapshot_is_rejected_by_version_cas(self):
        store = RedisSessionStore("redis://unused", redis_client=self._FakeRedis())
        session_id = new_id("ses")
        initial = store.load(session_id)
        self.assertIsNone(initial)

        from app.application.models import ConversationSession

        store.save(ConversationSession(id=session_id))
        first = store.load(session_id)
        stale = store.load(session_id)
        first.touch()
        store.save(first)
        stale.touch()
        with self.assertRaises(SessionConflictError) as ctx:
            store.save(stale)
        self.assertEqual(int(ctx.exception.code), 409)

    def test_redis_connection_failure_is_mapped_to_503(self):
        class BrokenRedis(self._FakeRedis):
            def get(self, key):
                raise ConnectionError("redis offline")

        store = RedisSessionStore("redis://unused", redis_client=BrokenRedis())
        with self.assertRaises(ServiceUnavailableError) as ctx:
            store.load(new_id("ses"))
        self.assertEqual(int(ctx.exception.code), 503)

    def test_redis_lock_timeout_is_retryable_business_error(self):
        class BusyLock:
            def acquire(self, **_kwargs):
                return False

        fake = self._FakeRedis()
        fake.lock = lambda *_args, **_kwargs: BusyLock()
        store = RedisSessionStore("redis://unused", redis_client=fake)
        with self.assertRaises(SessionLockTimeoutError) as ctx:
            with store.session_lock(new_id("ses")):
                pass
        self.assertEqual(int(ctx.exception.code), 429)

    def test_lost_redis_lock_rejects_session_write(self):
        class LostLock:
            def acquire(self, **_kwargs):
                return True

            def owned(self):
                return False

            def release(self):
                return None

        fake = self._FakeRedis()
        fake.lock = lambda *_args, **_kwargs: LostLock()
        store = RedisSessionStore("redis://unused", redis_client=fake)
        from app.application.models import ConversationSession

        session_id = new_id("ses")
        with self.assertRaises(SessionLockLostError) as ctx:
            with store.session_lock(session_id):
                store.save(ConversationSession(id=session_id))
        self.assertEqual(int(ctx.exception.code), 503)


class AssistantAPIRegressionTests(unittest.TestCase):
    def setUp(self):
        self._old_settings = ext.settings
        self._old_service = ext.application_service

    def tearDown(self):
        ext.settings = self._old_settings
        ext.application_service = self._old_service

    @staticmethod
    def _app() -> Flask:
        app = Flask(__name__)
        register_middlewares(app)
        app.register_blueprint(assistant_bp)
        return app

    def test_payload_too_large_returns_413_in_standard_envelope(self):
        ext.settings = SimpleNamespace(env="testing", assistant_api_key="", api_auth_enabled=False, api_auth_tokens="", api_auth_public_paths="/api/v1/health", rate_limit_enabled=False)
        response = self._app().test_client().post(
            "/api/v1/assistant/chat",
            data=b"x" * (2 * 1024 * 1024 + 1),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["code"], 413)

    def test_chat_rejects_malformed_or_non_object_json(self):
        ext.settings = SimpleNamespace(env="testing", assistant_api_key="", api_auth_enabled=False, api_auth_tokens="", api_auth_public_paths="/api/v1/health", rate_limit_enabled=False)
        client = self._app().test_client()
        malformed = client.post(
            "/api/v1/assistant/chat", data="{", content_type="application/json"
        )
        non_object = client.post(
            "/api/v1/assistant/chat", json=[{"message": "整体情况"}]
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(non_object.status_code, 400)

    def test_configured_assistant_api_key_is_required_and_accepts_bearer(self):
        class FakeService:
            def tools_meta(self):
                return {"tools": []}

        ext.settings = SimpleNamespace(env="testing", assistant_api_key="secret-key", api_auth_enabled=False, api_auth_tokens="", api_auth_public_paths="/api/v1/health", rate_limit_enabled=False)
        ext.application_service = FakeService()
        client = self._app().test_client()
        rejected = client.get("/api/v1/assistant/tools")
        self.assertEqual(rejected.status_code, 401)
        accepted = client.get(
            "/api/v1/assistant/tools",
            headers={"Authorization": "Bearer secret-key"},
        )
        self.assertEqual(accepted.status_code, 200)

    def test_production_without_assistant_key_fails_closed(self):
        ext.settings = SimpleNamespace(env="production", assistant_api_key="", api_auth_enabled=False, api_auth_tokens="", api_auth_public_paths="/api/v1/health", rate_limit_enabled=False)
        response = self._app().test_client().get("/api/v1/assistant/tools")
        self.assertEqual(response.status_code, 503)

    def test_first_service_build_is_singleton_under_concurrency(self):
        ext.settings = SimpleNamespace(env="testing", assistant_api_key="", api_auth_enabled=False, api_auth_tokens="", api_auth_public_paths="/api/v1/health", rate_limit_enabled=False)
        ext.application_service = None
        sentinel = object()
        build_count = 0

        def builder(*_args, **_kwargs):
            nonlocal build_count
            build_count += 1
            time.sleep(0.02)
            return sentinel

        with patch.object(
            application_api, "build_application_service", side_effect=builder
        ):
            with ThreadPoolExecutor(max_workers=8) as pool:
                instances = list(pool.map(lambda _: application_api._service(), range(16)))
        self.assertEqual(build_count, 1)
        self.assertTrue(all(item is sentinel for item in instances))


class ConversationRegressionTests(unittest.TestCase):
    def test_langchain_memory_adapter_enforces_message_limit(self):
        store = InMemorySessionStore(ttl_seconds=60, max_sessions=2)
        memory = LangChainSessionMemory(store, new_id("ses"), max_messages=4)
        for index in range(3):
            memory.save_context(
                {"input": f"question-{index}"},
                {"output": f"answer-{index}"},
            )
        session = memory.load_session()
        self.assertEqual(len(session.messages), 4)
        self.assertEqual(session.messages[0].content, "question-1")

    def test_explain_reuses_analysis_without_running_tool_again(self):
        service, client, _ = build_test_service()
        first = service.chat("预测一个老年人急诊入院5天的费用")
        call_count = len(client.calls)
        explained = service.chat("解释刚才的费用预测", session_id=first.session_id)
        self.assertEqual(explained.status, "context_summary")
        self.assertEqual(len(client.calls), call_count)
        self.assertEqual(explained.analysis["id"], first.analysis["id"])

    def test_reference_without_history_requests_clarification(self):
        service, _, _ = build_test_service()
        result = service.chat("解释刚才结果")
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.context["reason"], "missing_analysis_reference")

    def test_context_inheritance_updates_complete_intent_metadata(self):
        service, _, _ = build_test_service()
        first = service.chat("整体情况")
        continued = service.chat("继续", session_id=first.session_id)
        self.assertEqual(continued.intent["intent"], "statistics_overview")
        self.assertEqual(continued.intent["intent_label"], "平台总览统计")
        self.assertEqual(continued.intent["downstream"], "algorithm")
        self.assertEqual(continued.intent["downstream_target"], "statistics")

    def test_analysis_request_with_report_words_runs_new_analysis(self):
        service, client, _ = build_test_service()
        result = service.chat("按年龄段统计住院人次并生成报告")
        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.analysis)
        self.assertIsNotNone(result.report)
        self.assertTrue(client.calls)

    def test_report_negation_does_not_generate_report(self):
        service, client, _ = build_test_service()
        result = service.chat("按年龄段统计住院人次，不要生成报告")
        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.analysis)
        self.assertIsNone(result.report)
        self.assertTrue(client.calls)

    def test_explicit_unknown_session_does_not_silently_recreate_history(self):
        service, _, _ = build_test_service()
        with self.assertRaises(ResourceNotFoundError):
            service.chat("整体情况", session_id=new_id("ses"))

    def test_same_idempotency_key_with_different_payload_is_rejected(self):
        service, _, _ = build_test_service()
        first = service.chat("整体情况", request_id="request_12345678")
        with self.assertRaises(ConflictError) as ctx:
            service.chat(
                "按年龄段统计住院人次",
                session_id=first.session_id,
                request_id="request_12345678",
            )
        self.assertEqual(int(ctx.exception.code), 409)

    def test_delete_waits_for_inflight_chat_and_session_does_not_reappear(self):
        class BlockingClient(FakeAnalysisClient):
            def __init__(self):
                super().__init__()
                self.block = False
                self.started = threading.Event()
                self.release = threading.Event()

            def run_algorithm(self, name, params):
                if self.block:
                    self.started.set()
                    if not self.release.wait(timeout=3):
                        raise TimeoutError("test release timeout")
                return super().run_algorithm(name, params)

        client = BlockingClient()
        service, _, _ = build_test_service(client)
        first = service.chat("整体情况")
        client.block = True

        with ThreadPoolExecutor(max_workers=2) as pool:
            chatting = pool.submit(service.chat, "整体情况", session_id=first.session_id)
            self.assertTrue(client.started.wait(timeout=1))
            deleting = pool.submit(service.delete_session, first.session_id)
            time.sleep(0.05)
            self.assertFalse(deleting.done())
            client.release.set()
            self.assertEqual(chatting.result(timeout=3).status, "completed")
            self.assertTrue(deleting.result(timeout=3))

        with self.assertRaises(ResourceNotFoundError):
            service.get_session(first.session_id)


if __name__ == "__main__":
    unittest.main()
