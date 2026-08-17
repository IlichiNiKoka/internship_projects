# -*- coding: utf-8 -*-
"""意图识别训练 / 验证 / 测试集（需求 3.X.1 + 3.X.4）。

数据集说明：
  * 由人工编写，覆盖 7 个意图类别 + 模糊 / 多维度 / 医疗术语联想场景；
  * 训练集用于调优规则权重，验证集用于参数选择，测试集用于最终准确率评估；
  * 标注字段包括 expected_intent（期望意图）+ expected_params（期望抽取到的参数），
    便于准确率与抽取能力双指标评估；
  * 一期目标：测试集准确率 >= 90%（人员4 需求硬指标）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Sample:
    """单条标注样本。"""
    query: str                            # 用户自然语言输入
    expected_intent: str                  # 期望意图 key
    expected_params: dict = field(default_factory=dict)  # 期望抽取到的参数
    note: str = ""                        # 备注（解释为何这样标）


# ---------------------------------------------------------------------------
# 训练集（80 条）：用于规则权重调优
# ---------------------------------------------------------------------------
TRAIN_SET: list[Sample] = [
    # ---- aggregation_query（多维度聚合）----
    Sample("按年龄段统计住院人次", "aggregation_query",
           {"dimensions": ["age_group"], "metrics": ["discharge_count"]}),
    Sample("2021年各个医院的平均费用", "aggregation_query",
           {"dimensions": ["facility_name"], "metrics": ["avg_total_charges"],
            "filters": [{"field": "discharge_year", "op": "eq", "value": 2021}]}),
    Sample("按性别和年龄组分组的人数", "aggregation_query",
           {"dimensions": ["gender", "age_group"], "metrics": ["discharge_count"]}),
    Sample("各县的住院总费用合计", "aggregation_query",
           {"dimensions": ["hospital_county"], "metrics": ["sum_total_charges"]}),
    Sample("按疾病类型统计平均住院时长", "aggregation_query",
           {"dimensions": ["ccsr_diagnosis_description"], "metrics": ["avg_length_of_stay"]}),
    Sample("不同支付方式的住院人次", "aggregation_query",
           {"dimensions": ["payment_typology_1"], "metrics": ["discharge_count"]}),
    Sample("急诊入院的患者按年龄组统计", "aggregation_query",
           {"dimensions": ["age_group"], "metrics": ["discharge_count"],
            "filters": [{"field": "type_of_admission", "op": "eq", "value": "Emergency"}]}),
    Sample("按年份统计每年的人次", "aggregation_query",
           {"dimensions": ["discharge_year"], "metrics": ["discharge_count"]}),
    Sample("每个种族的平均住院天数", "aggregation_query",
           {"dimensions": ["race"], "metrics": ["avg_length_of_stay"]}),
    Sample("按 DRG 病组统计平均费用", "aggregation_query",
           {"dimensions": ["apr_drg_description"], "metrics": ["avg_total_charges"]}),
    Sample("内外科分别多少人次", "aggregation_query",
           {"dimensions": ["apr_medical_surgical_description"], "metrics": ["discharge_count"]}),
    Sample("按死亡风险分组的人次", "aggregation_query",
           {"dimensions": ["apr_risk_of_mortality"], "metrics": ["discharge_count"]}),
    Sample("各县按性别分组的住院人次", "aggregation_query",
           {"dimensions": ["hospital_county", "gender"], "metrics": ["discharge_count"]}),
    Sample("每个医院的平均成本", "aggregation_query",
           {"dimensions": ["facility_name"], "metrics": ["avg_total_costs"]}),
    Sample("2021年各年龄段的平均费用", "aggregation_query",
           {"dimensions": ["age_group"], "metrics": ["avg_total_charges"],
            "filters": [{"field": "discharge_year", "op": "eq", "value": 2021}]}),
    Sample("按出院去向分类的人次", "aggregation_query",
           {"dimensions": ["patient_disposition"], "metrics": ["discharge_count"]}),
    Sample("按医院服务区域统计", "aggregation_query",
           {"dimensions": ["hospital_service_area"], "metrics": ["discharge_count"]}),
    Sample("不同疾病严重程度的平均费用", "aggregation_query",
           {"dimensions": ["apr_severity_of_illness_description"], "metrics": ["avg_total_charges"]}),
    Sample("按 MDC 大类分组的人次", "aggregation_query",
           {"dimensions": ["apr_mdc_description"], "metrics": ["discharge_count"]}),
    Sample("每个邮编前缀的患者数", "aggregation_query",
           {"dimensions": ["zip_code_3_digits"], "metrics": ["discharge_count"]}),

    # ---- statistics_overview（总览统计）----
    Sample("整体情况怎么样", "statistics_overview"),
    Sample("平台总览", "statistics_overview"),
    Sample("给我看一下总体数据", "statistics_overview"),
    Sample("全平台的核心指标", "statistics_overview"),
    Sample("总体概览", "statistics_overview"),
    Sample("数据总览", "statistics_overview"),
    Sample("系统概况", "statistics_overview"),
    Sample("整体统计指标", "statistics_overview"),
    Sample("给我看下总人次和平均费用", "statistics_overview"),
    Sample("整体上急诊率多少", "statistics_overview"),
    Sample("平台概要", "statistics_overview"),
    Sample("全部数据的核心统计", "statistics_overview"),

    # ---- association_analysis（疾病关联）----
    Sample("肺炎常见的伴生操作有哪些", "association_analysis",
           {"antecedent": "ccsr_diagnosis_description"}),
    Sample("疾病和操作的关联", "association_analysis"),
    Sample("诊断和支付方式有什么关联", "association_analysis",
           {"antecedent": "ccsr_diagnosis_description", "consequent": "payment_typology_1"}),
    Sample("不同疾病的常见操作", "association_analysis"),
    Sample("疾病关联分析", "association_analysis"),
    Sample("什么病通常伴随着什么操作", "association_analysis"),
    Sample("诊断与治疗方式的关联规则", "association_analysis"),
    Sample("哪些疾病和入院类型经常一起出现", "association_analysis",
           {"consequent": "type_of_admission"}),
    Sample("病种和操作类型的关系", "association_analysis"),
    Sample("关联规则挖掘", "association_analysis"),

    # ---- cost_prediction（费用预测）----
    Sample("预测一下老年人的住院费用", "cost_prediction",
           {"mode": "predict", "sample": {"age_group": "70 or Older"}}),
    Sample("一个急诊入院5天的患者费用预测", "cost_prediction",
           {"mode": "predict",
            "sample": {"type_of_admission": "Emergency", "length_of_stay": 5}}),
    Sample("预测住院总费用", "cost_prediction", {"mode": "predict"}),
    Sample("费用预测模型效果如何", "cost_prediction", {"mode": "train"}),
    Sample("住院费用预估", "cost_prediction", {"mode": "predict"}),
    Sample("费用预测模型评估", "cost_prediction", {"mode": "train"}),
    Sample("预测一个内科老年人5天住院的费用", "cost_prediction",
           {"mode": "predict", "sample": {"age_group": "70 or Older",
                                          "apr_medical_surgical_description": "Medical",
                                          "length_of_stay": 5}}),
    Sample("评估费用预测模型", "cost_prediction", {"mode": "train"}),
    Sample("住院花费预测", "cost_prediction", {"mode": "predict"}),
    Sample("预测我住院要花多少钱", "cost_prediction", {"mode": "predict"}),

    # ---- readmission_risk（再入院风险）----
    Sample("哪些人群再入院风险高", "readmission_risk", {"mode": "profile"}),
    Sample("再入院风险评估", "readmission_risk", {"mode": "profile"}),
    Sample("评估一个老年人的再入院风险", "readmission_risk",
           {"mode": "score", "sample": {"age_group": "70 or Older"}}),
    Sample("再入院风险画像", "readmission_risk", {"mode": "profile"}),
    Sample("高风险人群特征", "readmission_risk", {"mode": "profile"}),
    Sample("风险评分", "readmission_risk", {"mode": "score"}),
    Sample("哪些患者容易再入院", "readmission_risk", {"mode": "profile"}),
    Sample("再住院风险", "readmission_risk", {"mode": "profile"}),
    Sample("评估这条记录的再入院风险", "readmission_risk", {"mode": "score"}),
    Sample("患者再入院风险评分", "readmission_risk", {"mode": "score"}),

    # ---- metadata_query（能力查询）----
    Sample("有哪些维度可以分析", "metadata_query", {"kind": "dimensions"}),
    Sample("支持哪些聚合指标", "metadata_query", {"kind": "metrics"}),
    Sample("有哪些算法可用", "metadata_query", {"kind": "algorithms"}),
    Sample("系统支持什么分析能力", "metadata_query"),
    Sample("能做什么分析", "metadata_query"),
    Sample("维度清单", "metadata_query", {"kind": "dimensions"}),
    Sample("指标清单", "metadata_query", {"kind": "metrics"}),
    Sample("算法清单", "metadata_query", {"kind": "algorithms"}),
    Sample("可用能力", "metadata_query"),
    Sample("告诉我系统能查询什么", "metadata_query"),

    # ---- unsupported（不支持范围）----
    Sample("今天天气怎么样", "unsupported"),
    Sample("帮我订一张机票", "unsupported"),
    Sample("给我讲个笑话", "unsupported"),
    Sample("你是谁", "unsupported"),
    Sample("今天几号", "unsupported"),
    Sample("翻译一下这句话", "unsupported"),
    Sample("北京有什么景点", "unsupported"),
    Sample("炒股票用什么策略", "unsupported"),
    Sample("推荐一本书", "unsupported"),
    Sample("怎么减肥", "unsupported"),
]


# ---------------------------------------------------------------------------
# 验证集（30 条）：用于参数选择
# ---------------------------------------------------------------------------
VALID_SET: list[Sample] = [
    # aggregation
    Sample("按年龄段统计住院人次", "aggregation_query",
           {"dimensions": ["age_group"], "metrics": ["discharge_count"]}),
    Sample("各医院 2021 年平均费用", "aggregation_query",
           {"dimensions": ["facility_name"], "metrics": ["avg_total_charges"]}),
    Sample("按性别分组的人次", "aggregation_query",
           {"dimensions": ["gender"], "metrics": ["discharge_count"]}),
    Sample("每个县的住院人次", "aggregation_query",
           {"dimensions": ["hospital_county"], "metrics": ["discharge_count"]}),
    Sample("不同年龄组的平均住院时长", "aggregation_query",
           {"dimensions": ["age_group"], "metrics": ["avg_length_of_stay"]}),
    # statistics
    Sample("平台总览数据", "statistics_overview"),
    Sample("整体概况", "statistics_overview"),
    Sample("全平台核心指标怎么样", "statistics_overview"),
    Sample("数据总览看一下", "statistics_overview"),
    Sample("总体统计", "statistics_overview"),
    # association
    Sample("疾病和操作的关联", "association_analysis"),
    Sample("诊断伴生操作", "association_analysis"),
    Sample("疾病关联分析", "association_analysis"),
    Sample("哪些操作经常和肺炎一起出现", "association_analysis"),
    Sample("关联规则", "association_analysis"),
    # cost
    Sample("预测住院费用", "cost_prediction", {"mode": "predict"}),
    Sample("费用预测模型评估", "cost_prediction", {"mode": "train"}),
    Sample("预估一个老年人住院的费用", "cost_prediction",
           {"mode": "predict", "sample": {"age_group": "70 or Older"}}),
    Sample("住院费用预测", "cost_prediction", {"mode": "predict"}),
    Sample("费用预测模型表现", "cost_prediction", {"mode": "train"}),
    # risk
    Sample("哪些人群再入院风险高", "readmission_risk", {"mode": "profile"}),
    Sample("再入院风险评分", "readmission_risk", {"mode": "score"}),
    Sample("高风险人群画像", "readmission_risk", {"mode": "profile"}),
    Sample("评估老年人再入院风险", "readmission_risk",
           {"mode": "score", "sample": {"age_group": "70 or Older"}}),
    Sample("再入院风险分析", "readmission_risk", {"mode": "profile"}),
    # metadata
    Sample("有哪些维度", "metadata_query", {"kind": "dimensions"}),
    Sample("支持哪些指标", "metadata_query", {"kind": "metrics"}),
    Sample("算法清单", "metadata_query", {"kind": "algorithms"}),
    Sample("系统能干什么", "metadata_query"),
    Sample("可用分析能力", "metadata_query"),
]


# ---------------------------------------------------------------------------
# 测试集（30 条）：最终准确率评估（要求 >= 90%）
# 含若干模糊 / 多维度 / 医疗术语联想样本（需求 3.X.4）
# ---------------------------------------------------------------------------
TEST_SET: list[Sample] = [
    # aggregation（多维度 + 模糊）
    Sample("各年龄段按性别统计一下", "aggregation_query",
           {"dimensions": ["age_group", "gender"], "metrics": ["discharge_count"]}),
    Sample("2021年每个医院的平均费用是多少", "aggregation_query",
           {"dimensions": ["facility_name"], "metrics": ["avg_total_charges"]}),
    Sample("按疾病类型和年龄组看人次", "aggregation_query",
           {"dimensions": ["ccsr_diagnosis_description", "age_group"],
            "metrics": ["discharge_count"]}),
    Sample("各县的住院总费用", "aggregation_query",
           {"dimensions": ["hospital_county"], "metrics": ["sum_total_charges"]}),
    Sample("不同性别患者平均住院多久", "aggregation_query",
           {"dimensions": ["gender"], "metrics": ["avg_length_of_stay"]}),
    Sample("按支付方式统计平均费用", "aggregation_query",
           {"dimensions": ["payment_typology_1"], "metrics": ["avg_total_charges"]}),
    Sample("急诊患者按年龄组统计", "aggregation_query",
           {"dimensions": ["age_group"], "metrics": ["discharge_count"]}),
    Sample("每个疾病严重度的平均费用", "aggregation_query",
           {"dimensions": ["apr_severity_of_illness_description"], "metrics": ["avg_total_charges"]}),
    Sample("2021年按县和性别分组的人次", "aggregation_query",
           {"dimensions": ["hospital_county", "gender"], "metrics": ["discharge_count"]}),
    Sample("每个 DRG 病组的平均住院时长", "aggregation_query",
           {"dimensions": ["apr_drg_description"], "metrics": ["avg_length_of_stay"]}),
    # statistics
    Sample("整体情况", "statistics_overview"),
    Sample("数据概况", "statistics_overview"),
    Sample("总览统计指标", "statistics_overview"),
    Sample("给我看下整体情况", "statistics_overview"),
    # association
    Sample("疾病和操作的关联分析", "association_analysis"),
    Sample("肺炎患者常见的操作", "association_analysis"),
    Sample("诊断与治疗方式关联", "association_analysis"),
    Sample("不同疾病经常伴随的操作", "association_analysis"),
    # cost
    Sample("预测一个老年人住院5天的费用", "cost_prediction",
           {"mode": "predict",
            "sample": {"age_group": "70 or Older", "length_of_stay": 5}}),
    Sample("费用预测模型表现怎么样", "cost_prediction", {"mode": "train"}),
    Sample("预估住院花费", "cost_prediction", {"mode": "predict"}),
    # risk
    Sample("哪些人群容易再入院", "readmission_risk", {"mode": "profile"}),
    Sample("再入院风险画像分析", "readmission_risk", {"mode": "profile"}),
    Sample("评估这个老年人的再入院风险", "readmission_risk",
           {"mode": "score", "sample": {"age_group": "70 or Older"}}),
    # metadata
    Sample("有哪些维度可以查询", "metadata_query", {"kind": "dimensions"}),
    Sample("支持哪些聚合指标", "metadata_query", {"kind": "metrics"}),
    Sample("系统能做什么分析", "metadata_query"),
    # unsupported
    Sample("今天天气如何", "unsupported"),
    Sample("帮我订机票", "unsupported"),
    Sample("讲个笑话", "unsupported"),
]


# ---------------------------------------------------------------------------
# 数据集汇总接口
# ---------------------------------------------------------------------------
def get_dataset(name: str) -> list[Sample]:
    """按名获取数据集：train / valid / test。"""
    return {
        "train": TRAIN_SET,
        "valid": VALID_SET,
        "test": TEST_SET,
    }[name]


def dataset_meta() -> dict:
    """供 /api/v1/ai/meta 暴露数据集规模。"""
    return {
        "train": len(TRAIN_SET),
        "valid": len(VALID_SET),
        "test": len(TEST_SET),
        "total": len(TRAIN_SET) + len(VALID_SET) + len(TEST_SET),
        "intents": sorted({s.expected_intent for s in TRAIN_SET + VALID_SET + TEST_SET}),
    }
