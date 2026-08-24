# -*- coding: utf-8 -*-
"""Prompt 设计（需求 3.X.2）。

设计原则：
  1. 系统 Prompt 锁定「医疗数据分析师」角色与输出格式约束；
  2. 用户 Prompt 通过模板注入结构化分析结果 + 业务上下文；
  3. 强化幻觉控制：明令「不得编造未给出的数字、不得引用未提供的数据」；
  4. 隐私脱敏：要求不输出任何可识别患者信息（姓名/身份证/医保号等）；
  5. 输出纯文本摘要 + 关键数字列表，便于前端二次渲染与幻觉检查。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 角色 Prompt：锁定 LLM 行为边界
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一名资深的医疗大数据分析师。

你的职责：把后端返回的结构化医疗数据分析结果，转写为非技术决策者可读的自然语言摘要。

【强约束：100% Grounded，杜绝超越数据】
1. 仅使用「分析结果」中真实存在的数字与字段。不得编造、不得引用未提供的统计数据，
   不得用外部常识或新闻补全；
2. 不得输出任何可识别患者个人隐私信息（姓名、医保号、身份证、出生日期、住址等）；
3. 摘要聚焦「业务结论」，避免堆砌技术指标或模型参数；
4. 关键数字保留整数与小数点后 2 位；货币默认保留整数；
5. 输出结构：
   - 第一段「摘要正文」100-300 字，仅基于分析结果做解读；
   - 第二段「关键数字」按「字段名: 数值」逐行列出；
   - （必要时）第三段「数据边界说明」：若用户原始查询包含本次分析结果不能回答的
     维度/指标/时间/领域，必须明确列出哪些部分未被覆盖，写
     「当前分析未覆盖：XXX。如需请重新指定维度或指标发起查询。」
6. 不得输出 markdown 表格、链接、图片，仅纯文本；
7. 若「分析结果」为空或全为 0，直接回复："当前没有可用的分析数据。"，不要编造业务结论；
8. 用户原始提问若超出本次分析结果维度（如要求"预测2025年"、"与北京数据对比"等），
   在「数据边界说明」中明确指出，不要推断或借用外部信息；
9. `risk_score` 是规则引擎的风险评分，不是事件发生概率；不得把它表述为"概率"或"预测发生率"。
"""


# ---------------------------------------------------------------------------
# 用户 Prompt 模板：注入业务上下文 + 结构化分析结果
# ---------------------------------------------------------------------------
USER_PROMPT_TEMPLATE = """用户原始查询：{user_query}
意图识别结果：{intent_label}
分析结果（JSON，仅此段内容可作为回答依据）：
{analysis_json}

请基于上面的「分析结果」生成中文摘要，遵循系统规则。
若用户查询中包含任何未出现在「分析结果」维度/指标的诉求，必须在「数据边界说明」中明确指出。
"""


def build_user_prompt(
    user_query: str,
    intent_label: str,
    analysis_result: Any,
    analysis_json: str,
) -> str:
    """组装用户 Prompt。"""
    return USER_PROMPT_TEMPLATE.format(
        user_query=user_query or "",
        intent_label=intent_label or "未识别",
        analysis_json=analysis_json,
    )


# ---------------------------------------------------------------------------
# Mock 客户端用的模板化文本生成（不调 LLM 也能产出摘要）
# ---------------------------------------------------------------------------
MOCK_TEMPLATES = {
    "aggregation_query": (
        "按 {dimensions} 维度对住院人次与相关指标进行了聚合统计，"
        "共返回 {row_count} 条分组结果。"
        "其中{top_row}是规模最大的分组。"
    ),
    "freeform_query": (
        "基于当前出院记录的维度与指标完成了自由问答聚合统计，"
        "共 {row_count} 条分组结果；"
        "如需不同维度或指标，请明确指定后重新发起查询。"
    ),
    "statistics_overview": (
        "平台总体共收录 {total_discharges} 例住院记录，"
        "平均住院时长 {avg_los} 天，平均费用 {avg_charges}，"
        "急诊入院占比 {emergency_rate}。"
    ),
    "association_analysis": (
        "挖掘到 {rule_count} 条关联规则，"
        "其中提升度最高的是「{top_rule}」，"
        "支持度 {support}，置信度 {confidence}。"
    ),
    "association_analysis_empty": "当前参数下未发现满足阈值的关联规则。",
    "cost_prediction_train": (
        "费用预测模型训练评估已完成，测试集 MAE={mae}，"
        "RMSE={rmse}，R²={r2}。"
    ),
    "cost_prediction_predict": (
        "已根据给定住院特征完成单样本费用预测，预测总费用约 "
        "{predicted_charge} {currency}。"
    ),
    "cost_prediction": "费用预测分析已完成，详细数值请查看结构化结果。",
    "readmission_risk_profile": (
        "再入院代理风险画像已完成，高风险人群比例为 {high_risk_rate}，"
        "高风险记录中数量最多的年龄组为 {top_group}。"
    ),
    "readmission_risk_score": (
        "给定住院特征的再入院代理风险评分为 {risk_score} 分，"
        "风险等级为 {risk_level}；该评分不是实际再入院概率。"
    ),
    "readmission_risk": "再入院代理风险分析已完成，详细数值请查看结构化结果。",
    "metadata_query": "已返回系统能力元数据，共 {item_count} 项。",
    "unsupported": "该查询不在医疗大数据分析范围内，无法生成摘要。",
}


def mock_render(intent: str, params: dict) -> str:
    """Mock 模式：不调 LLM，按模板渲染中文摘要。"""
    mode = params.get("mode")
    mode_key = f"{intent}_{mode}" if mode else intent
    template = MOCK_TEMPLATES.get(
        mode_key, MOCK_TEMPLATES.get(intent, MOCK_TEMPLATES["unsupported"])
    )
    try:
        return template.format(**{k: (v if v is not None else "—") for k, v in params.items()})
    except (KeyError, IndexError):
        # 字段缺失时退化为通用摘要
        return "已生成分析结果摘要（详细数字请参考原始 JSON 输出）。"
