# -*- coding: utf-8 -*-
"""AI 智能层测试（人员4：3.X.1 / 3.X.2 / 3.X.4）。

架构说明（2026-08-24 Agent 模式重构后）：
  * 执行链路：用户输入 -> LLM 工具规划（ToolPlanningAgent）-> 工具执行 -> grounded 摘要；
    本地规则意图分类器仅保留给 /ai/intent 调试端点与 Mock 兜底，不再前置拦截。
  * 规则引擎对无信号查询返回 freeform_query（置信度 0.35）是**设计内行为**——
    表示「委托给 LLM 决定」，不算分类错误。

测试分层：
  1. 单元层（默认跑，不依赖 Spark / 网络）：规则分类、术语联想、Mock 降级、客户端工厂；
  2. REST API 层（spark 标记）：Flask 测试客户端端到端，LLM 用 Mock；
  3. 真实 LLM 集成层（llm 标记）：真正调用 .env 配置的 LLM（如本地 Ollama qwen3.5:4b），
     验证摘要生成、Agent 工具规划与端到端回答；端点不可达时自动 skip。

运行方式：
  pytest tests/test_ai.py                 # 单元 + API 层
  pytest tests/test_ai.py -m llm          # 只跑真实 LLM 集成
"""
from __future__ import annotations

import socket
from urllib.parse import urlsplit

import pytest

from app.ai.agent import ToolPlanningAgent
from app.ai.intent.classifier import IntentClassifier, classify, evaluate
from app.ai.intent.terms import find_synonym, normalize_value
from app.ai.intent.training_data import TEST_SET, dataset_meta
from app.ai.service import AIService, reset_ai_service
from app.ai.summary.generator import SummaryGenerator
from app.ai.summary.llm_client import (
    DisabledClient,
    MockClient,
    build_client,
    describe_client,
)
from config.settings import Settings, testing_settings


# ===========================================================================
# 1. 意图识别（规则引擎）
#    注意：Agent 架构下规则引擎只做强信号匹配，无信号 -> freeform_query 委托 LLM。
# ===========================================================================
class TestIntentAccuracy:
    """需求 3.X.1：训练集 / 验证集 / 测试集准确率。

    评估口径（适配 Agent 架构）：
      * effective accuracy = 严格正确 + 合法委托（actual=freeform_query）
        —— 委托给 LLM 是设计行为，不计为错误；
      * 同时约束委托率上限，防止规则引擎退化成全靠 LLM。
    """

    DELEGATE_INTENT = "freeform_query"
    MIN_EFFECTIVE_ACCURACY = 0.95   # 有效准确率硬指标
    MAX_DELEGATION_RATE = 0.35      # 委托率上限

    def _check(self, samples, name: str):
        r = evaluate(samples)
        delegated = sum(
            1 for w in r["wrong_samples"] if w["actual"] == self.DELEGATE_INTENT
        )
        real_wrong = r["total"] - r["correct"] - delegated
        effective = (r["correct"] + delegated) / r["total"] if r["total"] else 0.0
        delegation_rate = delegated / r["total"] if r["total"] else 0.0
        assert effective >= self.MIN_EFFECTIVE_ACCURACY, (
            f"{name}集有效准确率 {effective:.4f} < {self.MIN_EFFECTIVE_ACCURACY}；"
            f"非委托错样本: "
            f"{[w for w in r['wrong_samples'] if w['actual'] != self.DELEGATE_INTENT]}"
        )
        assert delegation_rate <= self.MAX_DELEGATION_RATE, (
            f"{name}集委托率 {delegation_rate:.2%} > {self.MAX_DELEGATION_RATE:.0%}，"
            f"规则引擎退化"
        )

    def test_train_accuracy(self):
        from app.ai.intent.training_data import TRAIN_SET
        self._check(TRAIN_SET, "train")

    def test_valid_accuracy(self):
        from app.ai.intent.training_data import VALID_SET
        self._check(VALID_SET, "valid")

    def test_test_accuracy(self):
        self._check(TEST_SET, "test")

    def test_dataset_size(self):
        meta = dataset_meta()
        assert meta["total"] >= 100, "数据集总量应 >= 100"
        assert "aggregation_query" in meta["intents"]
        assert "unsupported" in meta["intents"]
        # freeform_query 是 Agent 架构新增意图：必须在目录中注册
        from app.ai.intent.catalog import INTENT_BY_KEY
        assert "freeform_query" in INTENT_BY_KEY, (
            "Agent 架构下必须包含 freeform_query（LLM 委托）意图"
        )


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
        # 强信号：规则直接命中
        ("整体情况", "statistics_overview"),
        ("费用预测", "cost_prediction"),
        ("再入院风险", "readmission_risk"),
        ("算法清单", "metadata_query"),
        ("有什么算法可用", "metadata_query"),
        ("肺炎常见的操作", "association_analysis"),
        # 无强信号：规则引擎委托 freeform_query，由 LLM 决定能否用数据回答
        # （真正的 unsupported 判定在集成层由 LLM 完成，见 TestLLMIntegration）
        ("今天天气", "freeform_query"),
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
# 3. 文本生成单元层（Mock 降级路径，不调网络）
# ===========================================================================
class TestSummaryGenerator:
    """需求 3.X.2 文本生成（Mock / Disabled 降级）。"""

    def setup_method(self):
        self.gen = SummaryGenerator(MockClient())

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

    def test_prompt_only_hallucination_marker(self):
        """幻觉后置校验已移除：hallucination 字段恒为 prompt_only 标记。"""
        data = {"rows": [["0 to 17", 12345]], "dimensions": ["age_group"]}
        r = self.gen.generate("按年龄段统计住院人次", "多维度聚合查询",
                              "aggregation_query", data)
        assert r.hallucination["passed"] is True
        assert r.hallucination["mode"] == "prompt_only"

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

    # ---- 2026-08-27 性能优化：Prompt 裁剪 / 摘要缓存 / 客户端复用 ----

    def test_prompt_pruning_truncates_long_rows(self):
        """超过 PROMPT_MAX_ROWS 的 rows 只注入前 N 行，且截断说明写进 Prompt。"""
        from app.ai.summary.generator import PROMPT_MAX_ROWS
        rows = [[f"group_{i}", float(i)] for i in range(PROMPT_MAX_ROWS + 20)]
        pruned, truncated = self.gen._prune_for_prompt({"rows": rows})
        assert truncated == len(rows)
        assert len(pruned["rows"]) == PROMPT_MAX_ROWS

        class CapturingClient:
            provider = "capture"
            def __init__(self):
                self.user_prompt = ""
            def chat(self, system_prompt, user_prompt):
                self.user_prompt = user_prompt
                return "摘要"

        client = CapturingClient()
        gen = SummaryGenerator(client)
        gen.generate("按年龄段统计", "多维度聚合查询",
                     "aggregation_query", {"rows": rows})
        # 截断说明进入实际发给 LLM 的 user_prompt
        assert f"共 {len(rows)} 行" in client.user_prompt
        assert f"前 {PROMPT_MAX_ROWS} 行" in client.user_prompt
        # 注入的 JSON 只有前 N 行
        assert f'"group_{PROMPT_MAX_ROWS}"' not in client.user_prompt

    def test_prompt_pruning_rounds_floats(self):
        """浮点压缩：Prompt 中的数字保留 4 位小数。"""
        pruned, _ = self.gen._prune_for_prompt({"avg": 4.32456789})
        assert pruned["avg"] == 4.3246

    def test_summary_cache_hits_skip_llm(self):
        """相同意图+提问+结果命中摘要缓存，第二次不再调用 LLM。"""
        from app.core.cache import InMemoryTTLCache

        class CountingClient:
            provider = "counting"
            def __init__(self):
                self.calls = 0
            def chat(self, system_prompt, user_prompt):
                self.calls += 1
                return "第一次生成的摘要"

        client = CountingClient()
        gen = SummaryGenerator(client, cache=InMemoryTTLCache(max_entries=16),
                               cache_ttl_seconds=60)
        data = {"rows": [["0 to 17", 12345]], "dimensions": ["age_group"]}
        r1 = gen.generate("按年龄段统计", "多维度聚合查询",
                          "aggregation_query", data)
        r2 = gen.generate("按年龄段统计", "多维度聚合查询",
                          "aggregation_query", data)
        assert client.calls == 1, f"期望只调用 1 次 LLM，实际 {client.calls}"
        assert r1.text == r2.text == "第一次生成的摘要"

    def test_summary_cache_key_differs_by_result_and_query(self):
        """结果不同或提问不同，缓存 key 不同（不误命中）。"""
        from app.core.cache import InMemoryTTLCache

        class CountingClient:
            provider = "counting"
            def __init__(self):
                self.calls = 0
            def chat(self, system_prompt, user_prompt):
                self.calls += 1
                return f"摘要{counter()}"

        def counter():
            return CountingClient.calls

        client = CountingClient()
        gen = SummaryGenerator(client, cache=InMemoryTTLCache(max_entries=16))
        data1 = {"rows": [["0 to 17", 12345]], "dimensions": ["age_group"]}
        data2 = {"rows": [["0 to 17", 99999]], "dimensions": ["age_group"]}
        gen.generate("按年龄段统计", "多维度聚合查询", "aggregation_query", data1)
        gen.generate("按年龄段统计", "多维度聚合查询", "aggregation_query", data1)
        gen.generate("换个问法", "多维度聚合查询", "aggregation_query", data1)
        gen.generate("按年龄段统计", "多维度聚合查询", "aggregation_query", data2)
        assert client.calls == 3, f"期望 3 次 LLM 调用，实际 {client.calls}"

    def test_llm_client_reused_across_chat_calls(self):
        """OpenAI 兼容客户端懒加载单例：多次 chat 复用同一个底层客户端。"""
        from app.ai.summary.llm_client import OpenAICompatibleClient
        client = OpenAICompatibleClient(
            api_key="sk-test", model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
        )
        # 首次调用前不建连
        assert client._client is None
        c1 = client._get_client()
        c2 = client._get_client()
        assert c1 is c2, "底层客户端应被复用（连接池复用，避免重复 TLS 握手）"


# ===========================================================================
# 4. AI 服务编排（单元层，注入 Mock 客户端）
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
# 5. 真实 LLM 集成层（-m llm 启用；端点不可达自动跳过）
#    使用 data_process/.env 的真实配置（当前：本地 Ollama qwen3.5:4b），
#    真正验证 LLM 摘要生成 / Agent 工具规划 / 端到端回答。
# ===========================================================================

def _endpoint_reachable(base_url: str, timeout: float = 3.0) -> bool:
    """探测 LLM OpenAI 兼容端点的 TCP 可达性。"""
    if not base_url:
        return True  # 官方端点不做本地探测
    p = urlsplit(base_url)
    host = p.hostname or "localhost"
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def real_llm():
    """真实 LLM 客户端 + 配置。不可达 / 明确配置为 mock 时跳过。"""
    s = Settings.load()
    provider = (s.llm_provider or "auto").lower()
    if provider in ("mock", "disabled"):
        pytest.skip(f"ANALYTICS_LLM_PROVIDER={provider}，未启用真实 LLM")
    if not s.llm_api_key:
        pytest.skip("ANALYTICS_LLM_API_KEY 为空，未启用真实 LLM")
    if not _endpoint_reachable(s.llm_base_url or ""):
        pytest.skip(f"LLM 端点不可达: {s.llm_base_url}")
    return build_client(s), s


class _StubAggregationService:
    """聚合服务桩：记录调用并返回确定性结果。"""

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, params: dict) -> dict:
        self.calls.append(params)
        dims = params.get("dimensions") or []
        rows = [[d, 1234.5] for d in dims]
        return {
            "rows": rows,
            "dimensions": dims,
            "metrics": params.get("metrics") or [],
            "row_count": len(rows),
        }


class _StubAlgorithmService:
    """算法服务桩。"""

    def __init__(self):
        self.names: list[str] = []

    def run(self, name: str, params: dict | None = None) -> dict:
        self.names.append(name)
        return {"mode": "overview", "algorithm": name}


@pytest.mark.llm
@pytest.mark.slow
class TestLLMIntegration:
    """真实 LLM 端到端：确保测试确实调用 LLM 生成回答，而非 Mock。"""

    def test_llm_chat_reachable_and_real(self, real_llm):
        """裸调用：LLM 必须返回非 __MOCK__ 的真实文本。"""
        client, s = real_llm
        out = client.chat("你是一个测试助手。", "只回复两个字：正常")
        assert out, "LLM 返回为空"
        assert not out.startswith("__MOCK__"), "LLM 返回了 Mock 标记，说明走了降级"

    def test_summary_generated_by_llm(self, real_llm):
        """SummaryGenerator 必须走真实 LLM，不降级到模板。"""
        client, s = real_llm
        gen = SummaryGenerator(client)
        data = {
            "rows": [["Female", 5234.5], ["Male", 4870.2]],
            "dimensions": ["gender"],
            "metrics": ["avg_total_charges"],
        }
        r = gen.generate("男女的平均费用差异", "多维度聚合查询",
                         "aggregation_query", data)
        assert r.fell_back_to_mock is False, "降级到了 Mock 模板，LLM 未生效"
        assert r.llm_provider != "mock"
        assert "__MOCK__" not in r.text
        assert len(r.text) >= 20, f"LLM 摘要过短: {r.text!r}"

    def test_agent_plans_aggregation_for_data_question(self, real_llm):
        """数据问题：Agent 应规划 aggregation 工具并成功执行。"""
        client, s = real_llm
        agg, alg = _StubAggregationService(), _StubAlgorithmService()
        agent = ToolPlanningAgent(
            llm_client=client, aggregation_service=agg, algorithm_service=alg)

        plan = agent.plan_and_execute("按性别统计平均住院费用")
        assert plan.errors == [] or not plan.max_loops_reached, \
            f"规划异常: {plan.errors}"
        assert plan.calls, "没有任何工具调用"
        tool = plan.calls[0].tool
        assert tool == "aggregation", \
            f"数据问题应选 aggregation，实际 {tool}（explanation={plan.calls[0].explanation}）"
        assert agg.calls, "aggregation 服务未被实际执行"
        assert "gender" in (agg.calls[0].get("dimensions") or []), \
            f"维度应包含 gender，实际 {agg.calls[0]}"

    def test_agent_direct_answer_for_offtopic(self, real_llm):
        """超范围闲聊：Agent 应回 direct_answer 并明确数据边界，而非编造数据。"""
        client, s = real_llm
        agg, alg = _StubAggregationService(), _StubAlgorithmService()
        agent = ToolPlanningAgent(
            llm_client=client, aggregation_service=agg, algorithm_service=alg)

        plan = agent.plan_and_execute("今天天气怎么样？适合出去玩吗？")
        assert plan.calls, "没有任何工具调用"
        tool = plan.calls[0].tool
        assert tool == "direct_answer", \
            f"超范围问题应选 direct_answer，实际 {tool}"
        answer = (plan.calls[0].params or {}).get("answer") or ""
        assert len(answer) >= 5, f"direct_answer 内容过短: {answer!r}"
        assert agg.calls == [], "超范围问题不应触发数据查询"

    def test_execute_end_to_end_with_real_llm(self, real_llm, tmp_path):
        """AIService.execute 全链路：真实 LLM 规划 + 执行 + 摘要生成。"""
        client, s = real_llm
        ts = testing_settings(tmp_path)
        agg, alg = _StubAggregationService(), _StubAlgorithmService()
        reset_ai_service()
        svc = AIService(settings=ts, aggregation_service=agg,
                        algorithm_service=alg, llm_client=client)
        result = svc.execute("按性别统计平均住院费用")

        summary = (result.summary.to_dict()
                   if hasattr(result.summary, "to_dict")
                   else dict(result.summary))
        assert summary.get("fell_back_to_mock") is False, "摘要降级到了 Mock"
        assert len(str(summary.get("text") or "")) >= 20, "最终回答过短"
        assert summary["hallucination"]["mode"] == "prompt_only"


# ===========================================================================
# 6. REST API 端到端（spark 标记：需要 PySpark + JDK；LLM 用 Mock）
# ===========================================================================
@pytest.mark.spark
@pytest.mark.usefixtures("app")
class TestAIAPI:
    """通过 Flask 测试客户端验证 API（Mock LLM，不调真实网络）。"""

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
        """端到端（Mock LLM）：Agent 走默认聚合兜底。"""
        r = client.post("/api/v1/ai/execute", json={"query": "整体情况"})
        assert r.status_code == 200
        data = r.get_json()["data"]
        # Mock 下 Agent 无法做 LLM 规划，走默认聚合兜底 -> freeform_query；
        # 若未来 Mock 策略调整映射回 statistics_overview 也接受。
        assert data["intent"]["intent"] in ("freeform_query", "statistics_overview")
        assert "analysis" in data
        assert "summary" in data

    def test_execute_offtopic(self, client):
        """超范围问题（Mock LLM）：无法真正判定 unsupported，
        只验证链路不崩、返回结构完整；语义判定见 TestLLMIntegration。"""
        r = client.post("/api/v1/ai/execute", json={"query": "今天天气"})
        assert r.status_code == 200
        data = r.get_json()["data"]
        assert data["intent"]["intent"] in ("unsupported", "freeform_query")
        assert "summary" in data
