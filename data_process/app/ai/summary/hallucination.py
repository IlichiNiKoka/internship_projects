# -*- coding: utf-8 -*-
"""摘要数字一致性校验（需求：校验摘要中的数字一致性）。

历史说明：
  该模块曾在 1a26ca8（Agent 工具规划重构）中随「幻觉后置校验」一起被移除，
  原因是旧版在聊天热路径上叠加了「本地数字比对 + LLM 事实核查」两次额外
  开销，延迟接近翻倍。

本次恢复策略（2026-08-25）：
  * 只恢复**本地确定性**的数字一致性比对 ``check()``，零网络、零 LLM 往返，
    开销仅为两次正则扫描；
  * 聊天热路径维持 prompt-only 约束不变（不回接本模块，保住延迟优化成果）；
  * 医疗洞察报告（reports.MedicalReportService）在汇总摘要时逐节调用本模块，
    数字不一致的摘要不纳入报告结论（fail-closed），并在 warnings 中给出
    未匹配数字明细。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 检查结果
# ---------------------------------------------------------------------------
@dataclass
class HallucinationReport:
    passed: bool                     # 是否通过校验
    source_numbers: list[float] = field(default_factory=list)     # 源分析结果中的数字
    generated_numbers: list[float] = field(default_factory=list)  # 生成文本中的数字
    unmatched: list[float] = field(default_factory=list)          # 文本中无法对应的数字

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "mode": "numeric_consistency",
            "source_count": len(self.source_numbers),
            "generated_count": len(self.generated_numbers),
            "unmatched": self.unmatched,
        }


# ---------------------------------------------------------------------------
# 数字抽取
# ---------------------------------------------------------------------------
def _extract_numbers_from_value(value: Any, sink: list[float]) -> None:
    """递归从 dict/list/number 中抽取可信源数字。

    特殊处理：
      * bool 不算数字（int 子类排除）；
      * dict/list 的长度也作为「合理推导数字」加入——LLM 文本常出现
        「N 条」「N 项」「N 个分组」这类对应集合大小的表述；
      * 字符串字段值中的数字同样纳入源集（如 "70 or Older"、"0 to 17"），
        否则摘要复述维度标签时会被误判为无中生有。
    """
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        sink.append(float(value))
    elif isinstance(value, dict):
        if value:
            sink.append(float(len(value)))
        for v in value.values():
            _extract_numbers_from_value(v, sink)
    elif isinstance(value, (list, tuple, set)):
        if value:
            sink.append(float(len(value)))
        for item in value:
            _extract_numbers_from_value(item, sink)
    elif isinstance(value, str):
        sink.extend(_extract_numbers_from_text(value))


# 匹配文本中的数字：
#   整数/浮点：123、12.5；千分位：1,000,000；带量词单位：12.5%、5天、10例、3995美元
NUM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])"                                # 不在标识符中
    r"(\d{1,3}(?:,\d{3})+|\d+\.\d+|\d+)"                # 数字主体
    r"\s*(?:万|亿|天|例|条|人|分|秒|%|岁|元|美元)?"
    r"(?![A-Za-z0-9_])"
)


def _extract_numbers_from_text(text: str) -> list[float]:
    """从生成文本中抽取所有数字（含千分位/百分号/货币与量词单位换算）。"""
    if not text:
        return []
    numbers: list[float] = []
    for m in NUM_PATTERN.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            num = float(raw)
        except ValueError:
            continue
        full = m.group(0)
        if "万" in full:
            num *= 10_000
        elif "亿" in full:
            num *= 100_000_000
        # 明显是年份的（1900-2099）不参与比对
        if 1900 <= num <= 2099 and "." not in m.group(1):
            continue
        # 明显是编码片段的超长整数不参与比对
        if num >= 1_000_000_000:
            continue
        numbers.append(num)
    return numbers


# ---------------------------------------------------------------------------
# 数值匹配（容忍小误差）
# ---------------------------------------------------------------------------
def _numbers_match(a: float, b: float, tolerance: float = 0.02) -> bool:
    """两个数字是否在容差内一致（默认 2% 相对误差，避免四舍五入误报）。"""
    if a == b:
        return True
    if abs(a) < 1e-9 and abs(b) < 1e-9:
        return True
    if abs(a) < 1e-9 or abs(b) < 1e-9:
        return abs(a - b) < 0.5  # 一边是 0，绝对差 < 0.5 视为一致
    return abs(a - b) / max(abs(a), abs(b)) <= tolerance


def check(source: Any, generated_text: str,
          tolerance: float = 0.02) -> HallucinationReport:
    """检查 LLM 生成文本中的数字是否与结构化分析结果一致。

    Args:
        source: 结构化分析结果（dict/list/标量）
        generated_text: LLM 生成的摘要文本
        tolerance: 相对误差容忍（默认 2%）

    Returns:
        HallucinationReport：passed=True 表示文本中的数字均能在源数据中找到。
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
