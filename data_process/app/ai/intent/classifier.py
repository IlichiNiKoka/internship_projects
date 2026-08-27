# -*- coding: utf-8 -*-
"""意图识别分类器（需求 3.X.1 基础 + 3.X.4 优化）。

实现策略（一期）：
  规则引擎 + 关键词匹配 + 模糊匹配 + 同义词联想，分层评分取最高分。
  在 TEST_SET 上准确率 >= 90%（见 tests/test_intent_classifier.py）。

二期可平滑升级为 ML 模型：
  * 训练数据已就绪（training_data.py）；
  * classify() 对外契约不变，仅替换内部实现即可。

能力覆盖：
  * 关键词触发：从 catalog / terms 词典中匹配意图暗示词；
  * 维度抽取：DIMENSION_KEYWORDS + VALUE_SYNONYMS；
  * 指标抽取：METRIC_KEYWORDS；
  * 过滤条件抽取：把「2021年」「急诊入院」「老年人」这类口语转 filters；
  * 多维度识别：同时抽取多个 dimension（3.X.4 多维度）；
  * 模糊查询：基于子串匹配 + 长度容差（3.X.4 大数据模糊查询）；
  * 医疗术语联想：synonym 反向索引（3.X.4 医疗术语联想）。

注：LLM 增强分类器（llm_classifier.py）与多意图识别已移除——Agent 架构下
意图理解直接委托给 LLM 工具规划，不再需要独立的 LLM 分类阶段。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.intent.catalog import INTENT_BY_KEY, IntentSpec, intent_meta
from app.ai.intent.terms import (
    ALGORITHM_KEYWORDS,
    DIMENSION_KEYWORDS,
    METADATA_KEYWORDS,
    METRIC_KEYWORDS,
    VALUE_SYNONYMS,
    normalize_value,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 识别结果契约
# ---------------------------------------------------------------------------
@dataclass
class IntentResult:
    """意图识别统一输出契约。"""
    query: str
    intent: str                     # 意图 key（unsupported 兜底）
    confidence: float               # 0.0 ~ 1.0
    params: dict = field(default_factory=dict)  # 抽取出的标准化参数
    missing_required: list[str] = field(default_factory=list)  # 缺失的必填参数
    matched_signals: dict[str, list[str]] = field(default_factory=dict)  # 调试：哪些信号触发了

    @property
    def spec(self) -> IntentSpec:
        return INTENT_BY_KEY[self.intent]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "intent": self.intent,
            "intent_label": self.spec.label_cn,
            "confidence": round(self.confidence, 4),
            "params": self.params,
            "missing_required": self.missing_required,
            "downstream": self.spec.downstream,
            "downstream_target": self.spec.target,
            "matched_signals": self.matched_signals,
        }


# ---------------------------------------------------------------------------
# 规则引擎核心
# ---------------------------------------------------------------------------
def _normalize_query(query: str) -> str:
    """统一处理输入：去多余空白、英文转小写（保留中文）。"""
    return re.sub(r"\s+", " ", query or "").strip()


def _score_text(text: str, keywords: list[str]) -> tuple[float, list[str]]:
    """文本中匹配关键词 -> (得分, 命中词列表)。"""
    if not text:
        return 0.0, []
    text_lower = text.lower()
    hits: list[str] = []
    score = 0.0
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in text_lower:
            hits.append(kw)
            # 长词权重更高（更具体）
            score += min(1.0, len(kw) / 4.0)
    return score, hits


def _extract_dimensions(query: str) -> tuple[list[str], list[str], list[dict]]:
    """抽取查询涉及的维度 + 命中关键词 + 隐含过滤条件。

    返回 (dimensions, matched_keywords, implied_filters)
    - dimensions：意图识别要查询的分组维度
    - implied_filters：用户提到的具体取值（如「2021年」「急诊」）转 filters
    """
    dims: list[str] = []
    matched_kw: list[str] = []
    filters: list[dict] = []

    # ---- 1. 维度关键词匹配 ----
    for dim, keywords in DIMENSION_KEYWORDS.items():
        _, hits = _score_text(query, keywords)
        if hits:
            if dim not in dims:
                dims.append(dim)
            matched_kw.extend(hits)

    # ---- 2. 同义词抽取具体取值 -> filters ----
    for dim, mapping in VALUE_SYNONYMS.items():
        for value, synonyms in mapping.items():
            for syn in synonyms:
                if syn.lower() in query.lower():
                    # 已是 dims 中的维度或新维度
                    if dim not in dims:
                        # 这种是「具体取值」而非「分组维度」：转 filter
                        filters.append({
                            "field": dim, "op": "eq",
                            "value": normalize_value(dim, value) or value,
                        })
                    else:
                        # 用户既问了维度又给了具体取值 -> 把具体取值也加为 filter
                        filters.append({
                            "field": dim, "op": "eq",
                            "value": normalize_value(dim, value) or value,
                        })
                    matched_kw.append(syn)
                    break  # 同一维度同一取值只记一次

    # ---- 3. 年份正则抽取 ----
    year_match = re.search(r"(20[12]\d|199\d)", query)
    if year_match:
        year = int(year_match.group(1))
        filters.append({"field": "discharge_year", "op": "eq", "value": year})
        if "discharge_year" not in dims:
            matched_kw.append(str(year))

    # ---- 4. 数值条件抽取（如「5天」「费用大于1万」）----
    los_match = re.search(r"(\d+)\s*天", query)
    if los_match:
        # 单条预测场景用得着；这里暂记入 params，由分类器决定是否用
        matched_kw.append(f"{los_match.group(1)}天")

    return dims, matched_kw, filters


def _extract_metrics(query: str) -> tuple[list[str], list[str]]:
    """抽取用户提到的指标关键词。"""
    metrics: list[str] = []
    matched: list[str] = []
    for metric, keywords in METRIC_KEYWORDS.items():
        _, hits = _score_text(query, keywords)
        if hits:
            metrics.append(metric)
            matched.extend(hits)
    return metrics, matched


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------
class IntentClassifier:
    """意图识别分类器。

    使用：
        classifier = IntentClassifier(min_confidence=0.45)
        result = classifier.classify("2021年各医院的平均费用")
        # result.intent == "aggregation_query"
        # result.params["dimensions"] == ["facility_name"]
        # result.params["filters"] == [{"field":"discharge_year","op":"eq","value":2021}]
    """

    def __init__(self, min_confidence: float = 0.45):
        self._min_confidence = min_confidence

    # ------------------------------------------------------------------
    def classify(self, query: str) -> IntentResult:
        """对用户自然语言输入做意图识别。

        策略：
        1. 规则引擎只做强信号匹配（关键词/维度/指标），不做基于主题的硬拒绝；
        2. 无明确信号时返回 freeform_query（置信度=0.35，LLM 增强分类器会自动接管），
           交由 LLM 基于完整维度/指标词典决定能否用数据回答，或确实判为 unsupported；
        3. 空输入单独兜底。
        """
        raw = query or ""
        text = _normalize_query(raw)
        signals: dict[str, list[str]] = {}
        params: dict[str, Any] = {}

        # ---- 0. 空输入 ----
        if not text:
            return self._build(raw, "unsupported", 0.0, params, signals, ["query"])

        # ---- 1. 抽取维度 / 指标 / 过滤 ----
        dims, dim_hits, filters = _extract_dimensions(text)
        metrics, metric_hits = _extract_metrics(text)
        if dim_hits:
            signals["dimensions"] = dim_hits
        if filters:
            signals["filters"] = [f"{f['field']}={f['value']}" for f in filters]
        if metrics:
            signals["metrics"] = metric_hits

        # ---- 2. 各意图打分 ----
        scores: dict[str, float] = {}
        matched_signals: dict[str, list[str]] = {}

        # 2.1 metadata：元数据关键词命中（强信号，单独命中即触发）
        meta_score, meta_hits = _score_text(text, METADATA_KEYWORDS)
        if meta_score > 0:
            scores["metadata_query"] = min(1.0, meta_score + 0.4)
            matched_signals["metadata_query"] = meta_hits

        # 2.2 statistics / association / cost_prediction / readmission_risk
        # 算法关键词；但需要避免「预测」「风险」这类词把单纯聚合查询也带偏
        algo_scores: dict[str, tuple[float, list[str]]] = {}
        for algo, keywords in ALGORITHM_KEYWORDS.items():
            s, hits = _score_text(text, keywords)
            if s > 0:
                algo_scores[algo] = (s, hits)

        # 统计/总览类关键词单独强匹配
        stat_kw = ["整体", "总览", "概览", "总体", "概况", "总统计", "数据总览",
                   "核心指标", "整体情况", "总体情况", "整体统计", "整体概况"]
        stat_s, stat_hits = _score_text(text, stat_kw)
        # 规则：有统计/总览词 + 没有"按/各/分组"模式 -> statistics_overview
        agg_pattern_for_stat = bool(re.search(
            r"(按.{0,8}统计|按.{0,8}分组|各.{0,4}的|每个.{0,4}的|分别.{0,4})",
            text))
        if stat_s > 0 and not agg_pattern_for_stat:
            # 用户没要按维度分组，纯要「总体」 -> statistics
            scores["statistics_overview"] = min(1.0, stat_s + 0.4)
            matched_signals["statistics_overview"] = stat_hits
        elif stat_s > 0 and "统计" in text and not dims:
            # 「整体统计」「总体统计」无维度 -> statistics
            scores["statistics_overview"] = min(1.0, stat_s + 0.2)
            matched_signals["statistics_overview"] = stat_hits

        if "statistics" in algo_scores:
            s, hits = algo_scores["statistics"]
            # 若同时有 metrics 但无 dims，statistics 也较高（"给我看下总人次和平均费用"）
            if not dims and metrics:
                scores["statistics_overview"] = max(
                    scores.get("statistics_overview", 0), s + 0.2)
            elif not agg_pattern_for_stat:
                scores["statistics_overview"] = max(
                    scores.get("statistics_overview", 0), s)
            matched_signals.setdefault("statistics_overview", []).extend(hits)

        if "association" in algo_scores:
            s, hits = algo_scores["association"]
            scores["association_analysis"] = min(1.0, s + 0.3)
            matched_signals["association_analysis"] = hits

        # 算法意图特判：cost_prediction
        # 关键：必须出现动作词（预测/预估/估计/predict）+ 主题词（费用/花费/cost）
        cost_action_words = ["预测", "预估", "估计", "predict"]
        has_cost_action = any(w in text.lower() for w in cost_action_words)
        has_cost_subject = ("费用" in text or "花费" in text or "cost" in text.lower())
        if "cost_prediction" in algo_scores and has_cost_action:
            s, hits = algo_scores["cost_prediction"]
            scores["cost_prediction"] = min(1.0, s + 0.4)
            matched_signals["cost_prediction"] = hits
        elif has_cost_action and has_cost_subject:
            # 「预估住院花费」「住院费用预估」这类：动作+主题
            scores["cost_prediction"] = 0.85
            matched_signals["cost_prediction"] = ["action+subject"]

        # 算法意图特判：readmission_risk
        # 必须出现 "再入院" 或 "再住院" 关键词（"风险" 单独太宽泛，会与"死亡风险"维度冲突）
        if "readmission_risk" in algo_scores:
            s, hits = algo_scores["readmission_risk"]
            scores["readmission_risk"] = min(1.0, s + 0.3)
            matched_signals["readmission_risk"] = hits

        # 2.3 aggregation_query：维度 / 指标 + 模式词
        agg_score = 0.0
        agg_pattern = bool(re.search(
            r"(按|各|每个|不同|分别|分组|分组统计|按.*统计|按.*分别)", text))
        if dims:
            agg_score += min(0.6, 0.25 * len(dims))
        if metrics and (agg_pattern or not dims):
            # 有分组模式词或无 dims 时 metrics 不强推 aggregation（让 statistics 接管）
            if agg_pattern:
                agg_score += min(0.4, 0.15 * len(metrics))
        if agg_pattern:
            agg_score += 0.35
            matched_signals.setdefault("aggregation_query", []).append("按/各/分组")
        # 防御：如果 stat_s 高且无 dims -> 优先 statistics，不进 aggregation
        if dims and stat_s > 0 and agg_pattern:
            # "按死亡风险分组的人次"：dims=[apr_risk_of_mortality] 但有"按...分组"
            # 强力推 aggregation 而非 readmission
            agg_score = max(agg_score, 0.85)
            matched_signals.setdefault("aggregation_query", []).append("按...分组模式")
        if agg_score > 0 and (dims or agg_pattern):
            scores["aggregation_query"] = min(1.0, agg_score)
            if dims:
                matched_signals.setdefault("aggregation_query", []).extend(
                    [f"dim:{d}" for d in dims])
            if metrics:
                matched_signals.setdefault("aggregation_query", []).extend(
                    [f"metric:{m}" for m in metrics])

        # ---- 3. 选择最高分意图 ----
        if not scores:
            # 规则引擎无信号：不直接拒绝，返回 freeform_query 让 LLM 接管，
            # 由 LLM 基于完整维度/指标词典决定能否映射到数据或确实 unsupported。
            signals["no_signal"] = ["rule_no_hit"]
            params = self._build_params("freeform_query", dims, metrics, filters, text)
            return self._build(raw, "freeform_query", 0.35, params, signals, ["rule_fallback_freeform"])

        top_intent = max(scores.items(), key=lambda kv: kv[1])
        intent, confidence = top_intent

        # 特例 1：「预测+费用」明确走 cost_prediction
        if has_cost_action and has_cost_subject and "cost_prediction" in scores:
            intent = "cost_prediction"
            confidence = scores["cost_prediction"]

        # 特例 2：「再入院/再住院」明确走 readmission_risk
        if "再入院" in text or "再住院" in text:
            if "readmission_risk" in scores:
                intent = "readmission_risk"
                confidence = scores["readmission_risk"]
            else:
                intent = "readmission_risk"
                confidence = 0.85
                matched_signals["readmission_risk"] = ["再入院关键词"]

        # 特例 3：association 模式识别："什么病通常...什么操作" / "肺炎常见的操作"
        # 规则：含疾病类关键词（病/诊断/肺炎/糖尿病/冠心病等具体病名）+ 操作类词，
        #       且不是 "按 X 统计 Y" 这种聚合模式
        if intent != "association_analysis":
            disease_word = bool(re.search(
                r"(病|诊断|肺炎|糖尿病|冠心病|疾病|病种|什么病|常见病)", text))
            operation_word = bool(re.search(r"(操作|手术|治疗方式|治疗|伴生|伴随)", text))
            # 排除：明显是聚合查询（含按/各/统计/分组 + 维度词）
            is_agg_pattern = bool(re.search(
                r"(按|各|每个|分别|分组|分组统计)", text)) and agg_pattern
            if disease_word and operation_word and not is_agg_pattern:
                intent = "association_analysis"
                confidence = max(scores.get("association_analysis", 0), 0.78)
                matched_signals["association_analysis"] = ["病+操作模式"]

        # 未达置信度阈值 -> 不直接拒答，交给 LLM 重新识别
        if confidence < self._min_confidence:
            params = self._build_params("freeform_query", dims, metrics, filters, text)
            return self._build(raw, "freeform_query", confidence, params, signals,
                              [f"rule_below_threshold({self._min_confidence})"])

        # ---- 4. 抽取参数 ----
        params = self._build_params(intent, dims, metrics, filters, text)

        return self._build(raw, intent, confidence, params, signals, list(matched_signals.get(intent, [])))

    # ------------------------------------------------------------------
    def _build_params(self, intent: str, dims: list[str], metrics: list[str],
                     filters: list[dict], text: str) -> dict:
        """根据意图类别组装对应参数。"""
        params: dict[str, Any] = {}

        if intent == "aggregation_query":
            if dims:
                params["dimensions"] = dims
            if metrics:
                params["metrics"] = metrics
            if filters:
                params["filters"] = filters

        elif intent == "statistics_overview":
            # top_n 可选，默认 10
            topn_match = re.search(r"(?:top|前)\s*(\d+)", text)
            if topn_match:
                params["top_n"] = int(topn_match.group(1))

        elif intent == "association_analysis":
            # 推断前件/后件
            antecedent = "ccsr_diagnosis_description"
            consequent = "ccsr_procedure_description"
            # 用户提到「支付方式」-> 后件换成支付方式
            if "支付" in text or "医保" in text:
                consequent = "payment_typology_1"
            if "入院类型" in text or "急诊入院" in text:
                consequent = "type_of_admission"
            params["antecedent"] = antecedent
            params["consequent"] = consequent

        elif intent == "cost_prediction":
            # 模式推断
            if "评估" in text or "训练" in text or "模型效果" in text or "表现" in text:
                params["mode"] = "train"
            else:
                params["mode"] = "predict"
                sample: dict[str, Any] = {}
                # 抽取单条样本特征
                for dim, mapping in VALUE_SYNONYMS.items():
                    for value, synonyms in mapping.items():
                        for syn in synonyms:
                            if syn.lower() in text.lower():
                                sample[dim] = normalize_value(dim, value) or value
                                break
                los_match = re.search(r"(\d+)\s*天", text)
                if los_match:
                    sample["length_of_stay"] = int(los_match.group(1))
                if "急诊" in text:
                    sample.setdefault("type_of_admission", "Emergency")
                if "内科" in text:
                    sample.setdefault("apr_medical_surgical_description", "Medical")
                elif "外科" in text:
                    sample.setdefault("apr_medical_surgical_description", "Surgical")
                if "老年" in text:
                    sample.setdefault("age_group", "70 or Older")
                if "极重" in text or "危重" in text:
                    sample.setdefault("apr_severity_of_illness_description", "Extreme")
                if sample:
                    params["sample"] = sample

        elif intent == "readmission_risk":
            if "评估" in text and ("这条" in text or "这个" in text or "老年人" in text):
                params["mode"] = "score"
                sample: dict[str, Any] = {}
                for dim, mapping in VALUE_SYNONYMS.items():
                    for value, synonyms in mapping.items():
                        for syn in synonyms:
                            if syn.lower() in text.lower():
                                sample[dim] = normalize_value(dim, value) or value
                                break
                if "老年" in text:
                    sample.setdefault("age_group", "70 or Older")
                if "急诊" in text:
                    sample.setdefault("type_of_admission", "Emergency")
                los_match = re.search(r"(\d+)\s*天", text)
                if los_match:
                    sample["length_of_stay"] = int(los_match.group(1))
                if sample:
                    params["sample"] = sample
            else:
                params["mode"] = "profile"

        elif intent == "metadata_query":
            if "维度" in text:
                params["kind"] = "dimensions"
            elif "指标" in text:
                params["kind"] = "metrics"
            elif "算法" in text:
                params["kind"] = "algorithms"

        elif intent == "freeform_query":
            # 规则引擎能抽到什么就先带上，LLM 会在增强分类阶段重新补全
            if dims:
                params["dimensions"] = dims
            if metrics:
                params["metrics"] = metrics
            if filters:
                params["filters"] = filters
            topn_match = re.search(r"(?:top|前)\s*(\d+)", text)
            if topn_match:
                params["top_n"] = int(topn_match.group(1))

        return params

    # ------------------------------------------------------------------
    def _build(self, query: str, intent: str, confidence: float,
               params: dict, signals: dict, reason: list[str]) -> IntentResult:
        spec = INTENT_BY_KEY[intent]
        missing = [p for p in spec.requires_params if p not in params]
        signals.setdefault(intent, []).extend(reason)
        return IntentResult(
            query=query,
            intent=intent,
            confidence=confidence,
            params=params,
            missing_required=missing,
            matched_signals=signals,
        )


# ---------------------------------------------------------------------------
# 单例入口
# ---------------------------------------------------------------------------
_classifier: IntentClassifier | None = None


def get_classifier(min_confidence: float = 0.45) -> IntentClassifier:
    global _classifier
    if _classifier is None or _classifier._min_confidence != min_confidence:
        _classifier = IntentClassifier(min_confidence=min_confidence)
    return _classifier


def classify(query: str, min_confidence: float = 0.45) -> IntentResult:
    """便捷函数：单次识别。"""
    return get_classifier(min_confidence).classify(query)


def evaluate(samples) -> dict:
    """评估分类器在样本集上的准确率（用于 90% 验证 & 调优）。"""
    correct = 0
    wrong: list[dict] = []
    for s in samples:
        result = classify(s.query)
        if result.intent == s.expected_intent:
            correct += 1
        else:
            wrong.append({
                "query": s.query,
                "expected": s.expected_intent,
                "actual": result.intent,
                "confidence": round(result.confidence, 3),
            })
    accuracy = correct / len(samples) if samples else 0.0
    return {
        "total": len(samples),
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "wrong_samples": wrong,
    }
