# -*- coding: utf-8 -*-
"""意图类别定义（需求 3.X.1 / 3.X.4）。

设计原则：
  1. 类别集合封闭可枚举，便于上层 AI 应用模块（人员1）做意图-API 映射；
  2. 每个意图声明它所依赖的下游能力（聚合 API / 算法组件 / 元数据 API），
     便于意图识别结果直接驱动后续工具调用；
  3. 「unsupported」是兜底类别，对应需求 3.X.1 备选流 2.2.3（输入不属于系统支持范围）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IntentSpec:
    """意图类别规格说明。"""
    key: str                       # 唯一键（API 输出字段值）
    label_cn: str                  # 中文名
    description: str               # 用户语义说明
    downstream: str                # 下游能力类型：aggregation / algorithm / metadata / none
    target: str | None = None      # 下游目标名（算法名 / 元数据类型；aggregation 时为 None）
    requires_params: tuple[str, ...] = ()  # 必须能从输入中抽取的参数
    optional_params: tuple[str, ...] = ()  # 可选参数


# ---------------------------------------------------------------------------
# 意图类别封闭集合
# ---------------------------------------------------------------------------
INTENTS: list[IntentSpec] = [
    IntentSpec(
        key="aggregation_query",
        label_cn="多维度聚合查询",
        description=(
            "用户希望按疾病/年龄/医院/年份/性别等维度对住院人次、平均费用、住院时长等"
            "指标做分组统计。例如「按年龄段统计住院人次」「2021年各医院的平均费用」"
        ),
        downstream="aggregation",
        requires_params=("dimensions",),
        optional_params=("metrics", "filters", "sort", "limit"),
    ),
    IntentSpec(
        key="statistics_overview",
        label_cn="平台总览统计",
        description=(
            "用户希望获取整体核心统计指标（总人次、平均住院、平均费用、急诊率等）"
            "与关键分布。例如「整体情况」「平台概览」「数据总览」"
        ),
        downstream="algorithm",
        target="statistics",
        requires_params=(),
        optional_params=("top_n",),
    ),
    IntentSpec(
        key="association_analysis",
        label_cn="疾病关联分析",
        description=(
            "用户希望挖掘疾病与操作/支付方式等的关联规则。"
            "例如「肺炎常见伴生什么操作」「疾病与支付方式的关联」"
        ),
        downstream="algorithm",
        target="association",
        requires_params=(),
        optional_params=("antecedent", "consequent", "min_support", "top_n"),
    ),
    IntentSpec(
        key="cost_prediction",
        label_cn="住院费用预测",
        description=(
            "用户希望基于住院特征预测住院费用。"
            "例如「预测一个老年人急诊入院5天的费用」"
        ),
        downstream="algorithm",
        target="cost_prediction",
        requires_params=(),
        optional_params=("mode", "sample", "sample_size", "train_ratio"),
    ),
    IntentSpec(
        key="readmission_risk",
        label_cn="再入院风险评估",
        description=(
            "用户希望评估患者再入院风险或查看人群风险画像。"
            "例如「哪些人群再入院风险高」「评估一条住院记录的风险」"
        ),
        downstream="algorithm",
        target="readmission_risk",
        requires_params=(),
        optional_params=("mode", "sample"),
    ),
    IntentSpec(
        key="metadata_query",
        label_cn="可用能力元数据查询",
        description=(
            "用户希望了解系统支持哪些维度、指标、算法。"
            "例如「有哪些维度」「能做什么分析」「支持哪些指标」"
        ),
        downstream="metadata",
        target=None,
        requires_params=(),
        optional_params=("kind",),  # dimensions / metrics / algorithms / cache
    ),
    IntentSpec(
        key="freeform_query",
        label_cn="自由形式数据问答",
        description=(
            "用户以任何形式提问,先尝试把问题映射到数据库可用维度与指标，"
            "作为聚合或总览查询执行；无法映射时由 LLM 基于数据决定能否回答，"
            "超出数据范围明确说明数据未覆盖。适用：任何与出院记录、医院、"
            "疾病、费用、住院时长、患者画像、年份等相关的开放式问题。"
        ),
        downstream="aggregation",
        requires_params=(),
        optional_params=("dimensions", "metrics", "filters", "sort", "limit", "top_n"),
    ),
    IntentSpec(
        key="unsupported",
        label_cn="不支持范围",
        description=(
            "问题与当前数据库完全无关（闲聊、天气、非医疗常识、编程代码等），"
            "无法通过现有工具或数据进行基于证据的回答；应礼貌说明数据范围后引导。"
        ),
        downstream="none",
        requires_params=(),
        optional_params=(),
    ),
]

INTENT_BY_KEY: dict[str, IntentSpec] = {i.key: i for i in INTENTS}

# ---------------------------------------------------------------------------
# 意图别名（Agent 模式 _alg_to_intent_key 会产出 *_query 后缀的历史键名，
# 若不在注册表中会导致 IntentResult.spec 抛 KeyError -> 500）。
# 别名统一指向等价 IntentSpec，对外契约（label/downstream/target）保持不变。
# ---------------------------------------------------------------------------
INTENT_ALIASES: dict[str, str] = {
    "cost_prediction_query": "cost_prediction",
    "readmission_risk_query": "readmission_risk",
    "aggregation_query": "freeform_query",
    "statistics_query": "statistics_overview",
    "association_query": "association_analysis",
}
for _alias, _canonical in list(INTENT_ALIASES.items()):
    if _alias not in INTENT_BY_KEY and _canonical in INTENT_BY_KEY:
        INTENT_BY_KEY[_alias] = INTENT_BY_KEY[_canonical]


def intent_meta() -> list[dict]:
    """供 /api/v1/ai/meta 接口对外暴露意图清单。"""
    return [
        {
            "key": i.key,
            "label": i.label_cn,
            "description": i.description,
            "downstream": i.downstream,
            "target": i.target,
            "requires_params": list(i.requires_params),
            "optional_params": list(i.optional_params),
        }
        for i in INTENTS
    ]
