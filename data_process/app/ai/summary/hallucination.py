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
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


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
