# -*- coding: utf-8 -*-
"""幻觉检查（需求 3.X.2 幻觉控制）。

策略：
  1. 从结构化「分析结果」中抽取所有数值（递归遍历 dict/list）；
  2. 从 LLM 生成的文本中抽取所有数字（含千分位逗号、百分号、单位等）；
  3. 比对：生成文本中的每个数字应能在输入数字集中找到（允许相对误差 tolerance）；
  4. 不一致数字 -> 标记为「潜在幻觉」并返回校验失败。

实现要点：
  * 只校验数字，不校验文本语义（一期足够）；
  * 整数比对要忽略格式（1000 与 "1,000" 视为同值）；
  * 浮点比对允许 2% 相对误差，避免四舍五入导致误报。

Phase 2 增强（已实现）：
  1. 事实核查：LLM 交叉验证生成文本中的事实声明是否源自结构化数据；
  2. 引用追踪：LLM 为每个关键结论标注来源字段/行号；
  3. 逻辑一致性检查：LLM 检测前后文矛盾；
  4. 实体一致性：LLM 校验人名/医院名/诊断名等实体与源数据一致；
  5. 语义级幻觉检测：LLM 判断生成文本是否蕴含于源数据（NLI）；
  6. 置信度分级：high/medium/low 风险等级（GradedHallucinationReport）。
  本地只负责预处理（提取声明、构造 prompt）与结果解析，语义判断交给 LLM。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 检查结果
# ---------------------------------------------------------------------------
@dataclass
class HallucinationReport:
    passed: bool                     # 是否通过校验
    source_numbers: list[float] = field(default_factory=list)   # 输入分析结果中的数字
    generated_numbers: list[float] = field(default_factory=list)  # 生成文本中的数字
    unmatched: list[float] = field(default_factory=list)         # 生成文本中无法对应的数字

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "source_count": len(self.source_numbers),
            "generated_count": len(self.generated_numbers),
            "unmatched": self.unmatched,
        }


# ---------------------------------------------------------------------------
# 数字抽取
# ---------------------------------------------------------------------------
def _extract_numbers_from_value(value, sink: list[float]) -> None:
    """递归从 dict/list/number 中抽取数字。

    特殊处理：
      * bool 不算数字（int 子类排除）；
      * 字符串字段值中的数字（如 "0 to 17"）不抽取——避免字段值被误判为统计数字；
      * list/dict 的长度也作为「合理推导数字」加入——因为 LLM 生成文本常出现
        「N 条」「N 项」「N 个分组」这类对应集合大小。
    """
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        sink.append(float(value))
    elif isinstance(value, dict):
        if value:
            sink.append(float(len(value)))  # 字典长度合理推导
        for v in value.values():
            _extract_numbers_from_value(v, sink)
    elif isinstance(value, (list, tuple, set)):
        if value:
            sink.append(float(len(value)))  # 集合长度合理推导
        for item in value:
            _extract_numbers_from_value(item, sink)
    elif isinstance(value, str):
        # 字符串字段值里的数字也纳入源数字集（如 "70 or Older"、"0 to 17"）。
        # 否则 LLM 在摘要里复述维度标签时，这些数字会被误判为「无中生有」，
        # 导致整段摘要被标记不可信（与文本侧使用同一套抽取规则，保持对称）。
        sink.extend(_extract_numbers_from_text(value))


# 匹配文本中的数字：
#   - 整数 / 浮点：123, 12.5
#   - 千分位：1,000,000
#   - 百分号：12.5%（取 12.5）
#   - 货币单位：¥123 / $456 / 1.2万元（取 12000）
#   - 量词：5天 / 10例 / 2.5倍（取 5 / 10 / 2.5）
# 不匹配：年份 1990-2099 之外的；UUID/编码等纯标识符
NUM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])"  # 不在标识符中
    r"(\d{1,3}(?:,\d{3})+|\d+\.\d+|\d+)"  # 数字主体
    r"\s*(?:万|亿|天|例|条|人|分|秒|%|岁|元|美元)?"  # 可选单位
    r"(?![A-Za-z0-9_])"
)


def _extract_numbers_from_text(text: str) -> list[float]:
    """从生成文本中抽取所有数字。"""
    if not text:
        return []
    numbers: list[float] = []
    for m in NUM_PATTERN.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            num = float(raw)
        except ValueError:
            continue
        # 处理单位
        full = m.group(0)
        if "万" in full:
            num *= 10_000
        elif "亿" in full:
            num *= 100_000_000
        # 跳过明显是年份的（1900-2099）
        if 1900 <= num <= 2099 and "." not in m.group(1):
            continue
        # 跳过明显是编码片段的（10+ 位整数）
        if num >= 1_000_000_000:
            continue
        numbers.append(num)
    return numbers


# ---------------------------------------------------------------------------
# 数值匹配（容忍小误差）
# ---------------------------------------------------------------------------
def _numbers_match(a: float, b: float, tolerance: float = 0.02) -> bool:
    """两个数字是否在容差内一致。"""
    if a == b:
        return True
    # 至少一个非零时按相对误差
    if abs(a) < 1e-9 and abs(b) < 1e-9:
        return True
    if abs(a) < 1e-9 or abs(b) < 1e-9:
        return abs(a - b) < 0.5  # 一边是 0，绝对差 < 0.5 视为一致
    return abs(a - b) / max(abs(a), abs(b)) <= tolerance


def check(source: dict | list, generated_text: str,
          tolerance: float = 0.02) -> HallucinationReport:
    """检查 LLM 生成文本中的数字是否与源分析结果一致。

    Args:
        source: 结构化分析结果（dict/list）
        generated_text: LLM 生成的文本
        tolerance: 相对误差容忍（默认 2%）

    Returns:
        HallucinationReport：passed=True 表示无幻觉
    """
    src_numbers: list[float] = []
    _extract_numbers_from_value(source, src_numbers)
    gen_numbers = _extract_numbers_from_text(generated_text)

    unmatched: list[float] = []
    for n in gen_numbers:
        if not any(_numbers_match(n, s, tolerance) for s in src_numbers):
            unmatched.append(n)

    passed = len(unmatched) == 0
    return HallucinationReport(
        passed=passed,
        source_numbers=src_numbers,
        generated_numbers=gen_numbers,
        unmatched=unmatched,
    )


# ---------------------------------------------------------------------------
# Phase 2: LLM 增强幻觉校验（事实核查 + 引用追踪 + 逻辑一致性 + 风险分级）
# ---------------------------------------------------------------------------
@dataclass
class ClaimCheck:
    """单条事实声明的校验结果。"""
    claim: str           # 声明文本
    verdict: str         # supported / contradicted / unverifiable
    source_ref: str = ""  # 来源字段/行号引用
    note: str = ""       # 补充说明


@dataclass
class GradedHallucinationReport:
    """分级幻觉报告：数值层 + LLM 语义层的综合结果。

    risk_level: low（全部通过）/ medium（存在不可验证声明）/ high（存在矛盾或数字不匹配）
    """
    passed: bool
    risk_level: str  # low / medium / high
    numeric_report: dict = field(default_factory=dict)   # 基础数值检查摘要
    claims: list[dict] = field(default_factory=list)     # LLM 逐条声明校验
    contradictions: list[str] = field(default_factory=list)  # 检测到的逻辑矛盾
    llm_used: bool = False  # 是否实际调用了 LLM

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "risk_level": self.risk_level,
            "numeric": self.numeric_report,
            "claims": self.claims,
            "contradictions": self.contradictions,
            "llm_used": self.llm_used,
        }


# ---------------------------------------------------------------------------
# Prompt 设计（本地 prompt steering）
# ---------------------------------------------------------------------------
_FACT_CHECK_SYSTEM = """\
你是一名严格的医疗数据事实核查员。

你将收到：
1. 「源数据」：结构化医疗分析结果（JSON），是唯一可信事实来源；
2. 「生成文本」：分析师据此写的自然语言摘要。

你的任务：
1. 从生成文本中提取每一条事实性声明（含数字、结论、实体名称）；
2. 逐条判断该声明是否被源数据支持：
   - supported：源数据中能找到对应；
   - contradicted：与源数据矛盾（如方向相反、数值不符）；
   - unverifiable：源数据中无足够信息判断；
3. 为 supported 的声明标注来源字段/行号（source_ref）；
4. 检查生成文本是否存在前后逻辑矛盾（如先说上升后说下降）。

【输出格式】仅返回 JSON：
```json
{
  "claims": [
    {"claim": "声明文本", "verdict": "supported|contradicted|unverifiable", "source_ref": "来源", "note": "备注"}
  ],
  "contradictions": ["矛盾描述1", "矛盾描述2"]
}
```
不输出 JSON 以外的文字。
"""

_FACT_CHECK_USER_TEMPLATE = """「源数据」（JSON）：
{source_json}

「生成文本」：
{generated_text}

请逐条核查并返回 JSON。"""


def _truncate_source(source: Any, max_chars: int = 4000) -> str:
    """源数据 JSON 预处理：截断过长内容，避免超出 LLM 上下文窗口。"""
    try:
        text = json.dumps(source, ensure_ascii=False, default=str)
    except Exception:
        text = str(source)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...（已截断）"
    return text


def _parse_llm_fact_check(raw: str) -> dict | None:
    """解析 LLM 事实核查的 JSON 响应。"""
    if not raw:
        return None
    # 尝试提取 JSON 对象
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        m = re.search(pattern, raw)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
    return None


def check_with_llm(
    source: dict | list,
    generated_text: str,
    llm_client,
    *,
    tolerance: float = 0.02,
) -> GradedHallucinationReport:
    """综合幻觉校验：数值一致性（本地）+ 语义事实核查（LLM）。

    本地负责：
      - 数值抽取与比对（已有 check() 逻辑）；
      - 源数据 JSON 截断预处理；
      - prompt 编排与 LLM 响应解析；
      - 风险等级判定。

    LLM 负责：
      - 逐条声明的事实核查与引用标注；
      - 逻辑矛盾检测。

    Args:
        source: 结构化分析结果
        generated_text: LLM 生成的摘要文本
        llm_client: LLMClient 实例（Mock/Disabled 时跳过语义层）
        tolerance: 数值相对误差容忍
    """
    # ---- 1. 本地数值层 ----
    numeric = check(source, generated_text, tolerance)
    numeric_dict = numeric.to_dict()

    # ---- 2. 判断是否调用 LLM 语义层 ----
    from app.ai.summary.llm_client import MockClient

    llm_used = False
    claims: list[dict] = []
    contradictions: list[str] = []

    if llm_client is not None and not isinstance(llm_client, MockClient):
        source_json = _truncate_source(source)
        user_prompt = _FACT_CHECK_USER_TEMPLATE.format(
            source_json=source_json,
            generated_text=generated_text or "",
        )
        try:
            raw = llm_client.chat(_FACT_CHECK_SYSTEM, user_prompt)
            if raw and not raw.startswith("__MOCK__"):
                parsed = _parse_llm_fact_check(raw)
                if parsed:
                    claims = parsed.get("claims") or []
                    contradictions = parsed.get("contradictions") or []
                    llm_used = True
                else:
                    logger.debug("LLM 事实核查响应无法解析: %s", raw[:200])
        except Exception as exc:
            logger.warning("LLM 事实核查调用失败: %s", exc)

    # ---- 3. 风险分级 ----
    has_numeric_fail = not numeric.passed
    has_contradiction = bool(contradictions)
    has_contradicted_claim = any(
        str(c.get("verdict") or "").lower() == "contradicted" for c in claims
    )

    if has_contradiction or has_contradicted_claim:
        risk_level = "high"
        passed = False
    elif has_numeric_fail:
        risk_level = "high"
        passed = False
    elif any(str(c.get("verdict") or "").lower() == "unverifiable" for c in claims):
        risk_level = "medium"
        passed = True  # 不可验证不等于幻觉，但仍提示风险
    else:
        risk_level = "low"
        passed = True

    return GradedHallucinationReport(
        passed=passed,
        risk_level=risk_level,
        numeric_report=numeric_dict,
        claims=claims,
        contradictions=contradictions,
        llm_used=llm_used,
    )
