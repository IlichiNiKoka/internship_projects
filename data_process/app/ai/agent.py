# -*- coding: utf-8 -*-
"""LLM 驱动的工具编排器（Agent）。

设计原则：
  1. 取消"本地意图识别"阶段，用户输入直接交给 LLM 做工具规划（tool use / function calling）。
     LLM 决定调用 Spark aggregation、algorithm 算法、metadata 元数据，或直接 grounded 文本回答。
  2. 本地只负责：
       - 工具契约 prompt 注入（维度/指标/过滤白名单 + algorithm 参数模板）
       - LLM 输出 JSON 安全解析与字段白名单校验
       - 工具调用（AggregationService / AlgorithmService），统一超时/异常捕获
       - 失败时最多 2 轮 self-correct（LLM 看错误信息重出参数）
  3. Text-to-SQL 模块不再生成裸 SQL；需要查数据时，
     LLM 直接输出 Spark aggregation 的结构化参数(dimensions/metrics/filters/...)，
     由本地调用聚合服务，安全、复用缓存、性能更稳。

产物：`ToolPlanningAgent.plan_and_execute(query, history) -> PlanResult`
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from config.registry import (
    DIMENSIONS,
    METRICS,
    NUMERIC_FILTER_COLUMNS,
    STRING_FILTER_OPS,
    FILTER_OPS,
    SORTABLE_FIELDS,
    dimension_meta,
    metric_meta,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 输出数据结构
# ---------------------------------------------------------------------------
@dataclass
class ToolCall:
    tool: str                       # "aggregation" / "algorithm" / "metadata" / "direct_answer"
    params: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""


@dataclass
class PlanResult:
    query: str
    calls: list[ToolCall] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)   # 每个 call 的执行结果
    errors: list[str] = field(default_factory=list)               # 每轮 LLM/执行 的错误信息
    max_loops_reached: bool = False
    elapsed_planning_seconds: float = 0.0
    elapsed_execution_seconds: float = 0.0

    @property
    def direct_answer(self) -> str | None:
        """如果 LLM 直接 grounded 文本回答（不调工具），返回文本。"""
        for call in self.calls:
            if call.tool == "direct_answer":
                return (call.params or {}).get("answer")
        return None

    def combined_analysis(self) -> dict[str, Any]:
        """把多轮调用结果打包成 generator.generate 可消费的 analysis_result 风格。"""
        if not self.results and not self.calls:
            return {"error": "no_tool_result", "calls": []}
        return {
            "agent": True,
            "calls": [
                {
                    "tool": c.tool,
                    "params": c.params,
                    "explanation": c.explanation,
                    "result": r,
                }
                for c, r in zip(self.calls, self.results)
            ],
            # generator 若需要单组聚合展示，默认取第一个 aggregation/algorithm 结果
            "primary": (self.results[0] if self.results else None),
        }


# ---------------------------------------------------------------------------
# Prompt 构造（本地预处理 + 契约 steering）
# ---------------------------------------------------------------------------
def _build_tools_table() -> str:
    """把 aggregation 维度/指标白名单 + 算法 + 元数据契约列出来，给 LLM 规划用。"""
    dim_lines = []
    for d in DIMENSIONS:
        note = f" [{d.value_type}]" if d.value_type != "string" else ""
        desc = f" — {d.description}" if d.description else ""
        dim_lines.append(f"- {d.key:<34s} ({d.label_cn}){note}{desc}")
    metric_lines = []
    for m in METRICS:
        unit = f" 单位:{m.unit}" if m.unit else ""
        col_src = f" 源列:{m.column}" if m.column else " COUNT(*)"
        desc = f" — {m.description}" if m.description else ""
        metric_lines.append(
            f"- {m.key:<28s} ({m.label_cn}) 聚合:{m.agg}{unit}{col_src}{desc}"
        )

    dims = "\n".join(dim_lines)
    mets = "\n".join(metric_lines)

    filter_ops = (
        f"- string 维度可用（STRING_FILTER_OPS）：{sorted(STRING_FILTER_OPS)}\n"
        f"- 数值类型（integer/double）额外可用：gte/gt/lte/lt/between，"
        f"整体 FILTER_OPS：{sorted(FILTER_OPS)}\n"
        f"- 可当作数值过滤但**不作为维度分组**的列（NUMERIC_FILTER_COLUMNS）："
        f"{sorted(NUMERIC_FILTER_COLUMNS)}\n"
    )

    sortable = (
        "允许排序的列 = 维度 key 与 指标 key 并集：\n"
        f"{sorted(SORTABLE_FIELDS)}\n"
    )

    return (
        "【聚合分析工具（aggregation）契约】\n"
        "params 字段：\n"
        "  dimensions: string[]        必填，1~5 个（必须是下方 DIMENSIONS 中的 key）\n"
        "  metrics: string[]           必填，至少 1 个（必须是下方 METRICS 中的 key）\n"
        "  filters: list[object]       可选，每项 {field: string, op: string, value: any}\n"
        "                                  field 必须是维度 key 或 NUMERIC_FILTER_COLUMNS 之一\n"
        "                                  op 必须来自 FILTER_OPS\n"
        "                                  between 的 value 为 [low, high]\n"
        "  sort: list[object]          可选，每项 {field: string, order: 'desc'|'asc'}\n"
        "                                  field 必须在 SORTABLE_FIELDS（维度/指标 key）\n"
        "  limit: number               可选，默认 100，最大 500\n\n"
        "【可用 DIMENSIONS（维度 key 与含义）】\n"
        f"{dims}\n\n"
        "【可用 METRICS（指标 key 与含义）】\n"
        f"{mets}\n\n"
        "【过滤与排序规则】\n"
        f"{filter_ops}\n"
        f"{sortable}\n"
        "\n"
        "【算法工具（algorithm）契约】\n"
        "params 格式：\n"
        "  name: string                必填，算法名，必须在以下列表中之一\n"
        "  params: object              可选，算法特定参数，示例见下\n\n"
        "  算法名（name）：\n"
        "    - statistics               平台总览统计，params 可选: {\"top_n\": 10}\n"
        "                               （各分布返回的头部条目数，1~50）\n"
        "    - association              疾病关联分析，params 全部可选:\n"
        "        {\"antecedent\": \"ccsr_diagnosis_description\",\n"
        "         \"consequent\": \"ccsr_procedure_description\",\n"
        "         \"min_support\": 0.005, \"top_n\": 20}\n"
        "        antecedent/consequent 可选值：ccsr_diagnosis_description / ccsr_diagnosis_code /\n"
        "        ccsr_procedure_description / ccsr_procedure_code / apr_mdc_description /\n"
        "        apr_severity_of_illness_description / type_of_admission / payment_typology_1 / age_group\n"
        "    - cost_prediction          住院费用预测，params 可选:\n"
        "        {\"mode\": \"train\", \"sample_size\": 100000, \"train_ratio\": 0.8,\n"
        "         \"sample\": {\"age_group\": \"30 to 49\", \"type_of_admission\": \"Emergency\",\n"
        "                    \"length_of_stay\": 5, \"apr_severity_of_illness_code\": 3}}\n"
        "        mode=train 训练评估；mode=predict 时 sample 必填\n"
        "    - readmission_risk         再入院风险评估，params 可选:\n"
        "        {\"mode\": \"profile\"}\n"
        "        mode=profile 人群画像；mode=score 单例评估（此时 sample 必填，同上）\n"
        "    - group_aggregation        分组聚合（一般优先用 aggregation 工具即可），params:\n"
        "        {\"dimensions\": [\"age_group\"], \"metrics\": [\"discharge_count\"]}\n"
        "        【元数据工具（metadata）契约】\n"
        "params: {\"kind\": \"dimensions\" | \"metrics\" | \"algorithms\"}\n"
        "  - dimensions: 返回 DIMENSIONS 维度白名单元数据\n"
        "  - metrics: 返回 METRICS 指标白名单元数据\n"
        "  - algorithms: 返回所有可用算法与参数说明\n\n"
        "【直接回答（direct_answer）契约】\n"
        "params: {\"answer\": string}  用于：\n"
        "  - 聊天打招呼/感谢/再见（你好、谢谢、再见），简短友好回应；\n"
        "  - 完全超出数据库能力范围的问题（编程、翻译、天气、新闻、SPARCS 2021 外的地域/年份等），\n"
        "    必须明确说明「当前数据库仅包含纽约州 2021 年出院记录，不覆盖此类信息」，\n"
        "    然后引导：可以尝试问一些与本平台数据相关的分析问题；\n"
        "  - 任何你判定用数据回答不了的问题，**禁止编造数字**。\n\n"
        "【规则】\n"
        "1. 回答严格 Grounded：不得编造维度/指标/数值，只能用工具查询到的结果做解读；\n"
        "2. 若问题含糊（如『最近怎么样』『医院有啥问题』），直接选默认 dimensions="
        "[\"discharge_year\", \"facility_name\"] + metrics=[\"discharge_count\", \"avg_total_charges\", \"avg_length_of_stay\"]"
        " 发起 aggregation，再解读；不要让用户澄清；\n"
        "3. 不要调用 unsupported / clarification 工具；要么调用上述 3 种工具，要么 direct_answer；\n"
        "4. 最多输出 1 个工具调用（本系统串行执行，不需要并行）；不要输出 calls 数组为空。\n"
    )


SYSTEM_PROMPT_PREFIX = """你是一个基于 SPARCS 出院记录的医疗数据分析 Agent。

你的任务：理解用户问题 → 选择最合适的工具（或直接 grounded 文本回答）→ 输出严格的 JSON。
本系统取消了"本地意图识别"，由你全权决定调用哪个工具、传什么参数。

【工具与契约】
{tools_table}

【输出格式】只输出一段 JSON，不要 markdown、不要代码块、不要额外解释：
```
{{
  "call": {{
    "tool": "aggregation" | "algorithm" | "metadata" | "direct_answer",
    "params": {{ ... }},
    "explanation": "一句话说明你为什么做这个决定"
  }}
}}
```
"""

USER_PROMPT_TEMPLATE = """{history_block}
用户问题：{query}

请严格按系统规则输出单段 JSON（不要 ``` 包裹）。"""


# ---------------------------------------------------------------------------
# 解析 LLM 输出
# ---------------------------------------------------------------------------
_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_plan(raw: str) -> ToolCall | None:
    if not raw or raw.startswith("__MOCK__"):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[\w]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    call = obj.get("call")
    if isinstance(call, dict):
        tool = call.get("tool")
        params = call.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        expl = call.get("explanation") or ""
        if isinstance(expl, (dict, list)):
            expl = json.dumps(expl, ensure_ascii=False)
        if tool in ("aggregation", "algorithm", "metadata", "direct_answer"):
            return ToolCall(tool=tool, params=params, explanation=str(expl))
    return None


# ---------------------------------------------------------------------------
# 参数校验（本地第二道防线，LLM 会犯错）
# ---------------------------------------------------------------------------
def _validate_agg_params(params: dict[str, Any]) -> tuple[dict, str | None]:
    """校验并修复 aggregation params，返回 (cleaned, err)。

    修复策略：缺维度补默认维度，缺指标补默认指标，非法字段直接剔除，
    使得绝大多数"参数不对"情况仍能顺利进入 Spark 执行。
    """
    dims = params.get("dimensions") or []
    if isinstance(dims, str):
        dims = [dims]
    valid_dims = {d.key for d in DIMENSIONS}
    dims = [d for d in dims if isinstance(d, str) and d in valid_dims]
    if not dims:
        dims = ["discharge_year", "facility_name"]
    dims = list(dict.fromkeys(dims))[:5]  # 去重，最多 5 个

    mets = params.get("metrics") or []
    if isinstance(mets, str):
        mets = [mets]
    valid_mets = {m.key for m in METRICS}
    mets = [m for m in mets if isinstance(m, str) and m in valid_mets]
    if not mets:
        mets = ["discharge_count", "avg_total_charges", "avg_length_of_stay"]
    mets = list(dict.fromkeys(mets))

    # filters
    cleaned_filters: list[dict] = []
    all_filter_fields = valid_dims | set(NUMERIC_FILTER_COLUMNS)
    for item in params.get("filters") or []:
        if not isinstance(item, dict):
            continue
        fld = item.get("field")
        op = item.get("op")
        if not (isinstance(fld, str) and fld in all_filter_fields and isinstance(op, str)):
            continue
        # 区分 string/numeric op
        dim = next((d for d in DIMENSIONS if d.key == fld), None)
        if dim is not None:
            allowed = STRING_FILTER_OPS if dim.value_type == "string" else FILTER_OPS
        else:
            allowed = FILTER_OPS
        if op not in allowed:
            continue
        if op == "between":
            v = item.get("value")
            if not (isinstance(v, (list, tuple)) and len(v) == 2):
                continue
            cleaned_filters.append({"field": fld, "op": op, "value": list(v)})
        elif op in ("in", "not_in"):
            v = item.get("value")
            if not isinstance(v, (list, tuple)):
                continue
            cleaned_filters.append({"field": fld, "op": op, "value": list(v)[:200]})
        elif "value" not in item:
            continue
        else:
            cleaned_filters.append({"field": fld, "op": op, "value": item.get("value")})

    # sort
    cleaned_sort: list[dict] = []
    for item in params.get("sort") or []:
        if not isinstance(item, dict):
            continue
        fld = item.get("field")
        order = (item.get("order") or "desc").lower()
        if not (isinstance(fld, str) and fld in SORTABLE_FIELDS and order in ("desc", "asc")):
            continue
        cleaned_sort.append({"field": fld, "order": order})
    if not cleaned_sort and mets:
        cleaned_sort = [{"field": mets[0], "order": "desc"}]

    # limit
    try:
        limit = int(params.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(500, limit))

    cleaned = {
        "dimensions": dims,
        "metrics": mets,
        "filters": cleaned_filters,
        "sort": cleaned_sort,
        "limit": limit,
    }
    # 可选保留 page/page_size
    if isinstance(params.get("page"), int) and params["page"] >= 1:
        cleaned["page"] = params["page"]
        psz = params.get("page_size") or limit
        try:
            cleaned["page_size"] = max(1, min(500, int(psz)))
        except (TypeError, ValueError):
            cleaned["page_size"] = limit

    return cleaned, None


_VALID_ALG_NAMES = {
    "statistics", "association", "cost_prediction",
    "readmission_risk", "group_aggregation",
}


def _validate_alg_params(params: dict[str, Any]) -> tuple[dict, str | None]:
    name = params.get("name")
    # 历史别名归一：早期提示词曾把平台总览写作 statistics_overview，
    # 兼容模型惯性输出旧名，映射到注册表真名 statistics。
    if isinstance(name, str) and name.strip() == "statistics_overview":
        name = "statistics"
    if not isinstance(name, str) or name not in _VALID_ALG_NAMES:
        return {}, f"algorithm name 非法，允许：{sorted(_VALID_ALG_NAMES)}"
    sub = params.get("params") or {}
    if not isinstance(sub, dict):
        sub = {}
    return {"name": name, "params": sub}, None


def _validate_meta_params(params: dict[str, Any]) -> tuple[dict, str | None]:
    kind = params.get("kind")
    if kind not in ("dimensions", "metrics", "algorithms"):
        kind = "metrics"
    return {"kind": kind}, None


def validate_call(call: ToolCall) -> tuple[ToolCall, str | None]:
    if call.tool == "aggregation":
        cleaned, err = _validate_agg_params(call.params or {})
        return ToolCall(call.tool, cleaned, call.explanation), err
    if call.tool == "algorithm":
        cleaned, err = _validate_alg_params(call.params or {})
        return ToolCall(call.tool, cleaned, call.explanation), err
    if call.tool == "metadata":
        cleaned, err = _validate_meta_params(call.params or {})
        return ToolCall(call.tool, cleaned, call.explanation), err
    # direct_answer
    ans = (call.params or {}).get("answer") or ""
    if not isinstance(ans, str):
        ans = str(ans)
    if len(ans) > 2000:
        ans = ans[:2000]
    return ToolCall(call.tool, {"answer": ans}, call.explanation), None


# ---------------------------------------------------------------------------
# 顶层 Agent
# ---------------------------------------------------------------------------
class ToolPlanningAgent:
    """LLM 工具规划 + 本地执行 + 最多 2 轮 self-correct。"""

    def __init__(self, llm_client, aggregation_service, algorithm_service, *,
                 max_correct_loops: int = 2):
        self._llm = llm_client
        self._agg = aggregation_service
        self._alg = algorithm_service
        self._max_correct_loops = max(0, int(max_correct_loops))

    # ------------------------------------------------------------------
    def plan_and_execute(self, query: str, *, history: str = "") -> PlanResult:
        import time
        from app.ai.summary.llm_client import MockClient

        res = PlanResult(query=query)
        client = self._llm
        is_mock = client is None or isinstance(client, (MockClient,))

        tools_table = _build_tools_table()
        sys_prompt = SYSTEM_PROMPT_PREFIX.format(tools_table=tools_table)

        his_block = ""
        if history:
            his_block = f"【上下文对话历史摘要】\n{history}\n"
        usr_prompt = USER_PROMPT_TEMPLATE.format(history_block=his_block, query=query)

        call: ToolCall | None = None
        loops_done = 0

        # ---- Planning + Self-correct 循环 ----
        plan_start = time.perf_counter()
        while True:
            if loops_done > self._max_correct_loops:
                res.max_loops_reached = True
                res.errors.append("达到最大 self-correct 次数")
                break

            # LLM 规划（Mock 时走默认聚合，阻塞用户体验可接受）
            if is_mock:
                raw = None
            else:
                try:
                    raw = client.chat(sys_prompt, usr_prompt)
                except Exception as exc:
                    logger.warning("Agent LLM 调用失败 loop=%s: %s", loops_done, exc)
                    res.errors.append(f"LLM 调用失败: {type(exc).__name__}")
                    # 最后一次 LLM 失败 -> 走默认 direct_answer/默认聚合
                    raw = None

            parsed = _parse_plan(raw) if raw else None
            if parsed is None:
                # LLM 不可用或解析失败：默认用最通用的 freeform aggregation 兜底
                parsed = ToolCall(
                    tool="aggregation",
                    params={
                        "dimensions": ["discharge_year", "facility_name"],
                        "metrics": ["discharge_count", "avg_total_charges", "avg_length_of_stay"],
                        "sort": [{"field": "discharge_count", "order": "desc"}],
                        "limit": 20,
                    },
                    explanation="LLM 不可用或解析失败，默认聚合兜底",
                )

            validated, err = validate_call(parsed)
            if err is not None:
                loops_done += 1
                res.errors.append(f"loop{loops_done} 参数校验：{err}")
                # 把错误塞回 prompt，让 LLM self-correct
                usr_prompt = (
                    f"{USER_PROMPT_TEMPLATE.format(history_block=his_block, query=query)}\n\n"
                    f"⚠ 上次规划的参数校验失败：{err}。请修正后重新输出 JSON。"
                )
                continue

            call = validated
            break

        res.elapsed_planning_seconds = round(time.perf_counter() - plan_start, 4)

        # ---- 执行 ----
        if call is None:
            return res
        res.calls = [call]

        exec_start = time.perf_counter()
        try:
            result = self._execute(call)
            res.results = [result]
        except Exception as exc:
            logger.warning("Agent 工具执行失败: %s", exc, exc_info=False)
            res.results = [{"error": f"{type(exc).__name__}: {exc}"}]
            res.errors.append(f"执行失败: {type(exc).__name__}: {exc}")
        res.elapsed_execution_seconds = round(time.perf_counter() - exec_start, 4)
        return res

    # ------------------------------------------------------------------
    def _execute(self, call: ToolCall) -> dict[str, Any]:
        if call.tool == "aggregation":
            if self._agg is None:
                return {"error": "aggregation service 未注入", "params": call.params}
            return self._agg.run(call.params)
        if call.tool == "algorithm":
            if self._alg is None:
                return {"error": "algorithm service 未注入", "params": call.params}
            return self._alg.run(call.params["name"], call.params.get("params") or {})
        if call.tool == "metadata":
            kind = call.params.get("kind")
            if kind == "dimensions":
                return {"kind": "dimensions", "data": dimension_meta()}
            if kind == "metrics":
                return {"kind": "metrics", "data": metric_meta()}
            if kind == "algorithms":
                return {
                    "kind": "algorithms",
                    "data": [
                        {"name": "statistics", "description": "平台总览统计（默认年份 2021）"},
                        {"name": "association", "description": "疾病/操作与维度的关联分析"},
                        {"name": "cost_prediction", "description": "基于患者特征预测住院费用"},
                        {"name": "readmission_risk", "description": "再入院风险评估（人群或单例）"},
                        {"name": "group_aggregation", "description": "按分组聚合（与 aggregation 同义）"},
                    ],
                }
            return {"error": f"未知 metadata kind: {kind}"}
        # direct_answer：LLM 已经写好 answer，直接返回
        return {"mode": "direct_answer", "answer": (call.params or {}).get("answer", "")}
