# -*- coding: utf-8 -*-
"""医疗术语范围与同义词词典（需求 3.X.4 医疗术语联想）。

设计：
  1. 每个 SPARCS 维度的合法取值，均给出中文/英文/口语化同义词 -> 标准取值 的映射；
  2. 维度关键词（哪个维度在问）与取值关键词（具体取值是啥）分离；
  3. 词典可被 classifier 直接消费，也可对外暴露供前端联想输入。

字段命名严格对齐清洗后数据字典（data_dictionary.md / config/registry.py）。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 维度关键词：自然语言中可能表达某个维度的词 -> dimension key（见 config/registry.py）
# 顺序影响优先级（越靠前优先匹配）
# ---------------------------------------------------------------------------
DIMENSION_KEYWORDS: dict[str, list[str]] = {
    # 时间
    "discharge_year":       ["年份", "年度", "year", "哪年", "哪一年", "2021", "2022"],
    # 患者画像
    "age_group":           ["年龄", "年龄段", "年纪", "老人", "儿童", "小孩", "老年",
                            "中年", "青年", "少年", "age", "岁"],
    "gender":              ["性别", "男女", "男", "女", "gender", "sex"],
    "race":                ["种族", "race"],
    "ethnicity":           ["族裔", "民族", "西班牙裔", "hispanic"],
    "zip_code_3_digits":   ["邮编", "邮政编码", "zip", "zipcode"],
    # 机构/地域
    "hospital_service_area": ["服务区域", "区域", "service area"],
    "hospital_county":    ["县", "医院县", "所在县", "county"],
    "facility_name":      ["医院", "机构", "医疗机构", "facility", "hospital"],
    # 疾病/诊疗
    "ccsr_diagnosis_description": ["疾病", "病种", "诊断", "什么病", "disease", "diagnosis"],
    "ccsr_procedure_description": ["操作", "手术", "procedure", "治疗方式"],
    "apr_drg_description": ["DRG", "drg", "病组"],
    "apr_mdc_description": ["MDC", "mdc", "大类"],
    "apr_severity_of_illness_description": ["病情", "严重程度", "severity", "严重度"],
    "apr_risk_of_mortality": ["死亡风险", "mortality", "死亡率"],
    "apr_medical_surgical_description": ["内外科", "medical", "surgical", "外科", "内科"],
    # 住院过程
    "type_of_admission":  ["入院类型", "入院方式", "急诊入院", "择期", "admission", "急诊", "elective"],
    "patient_disposition": ["出院去向", "出院", "disposition", "去向"],
    "emergency_department_indicator": ["急诊", "急诊科", "emergency", "ed"],
    # 支付
    "payment_typology_1": ["支付方式", "支付", "医保", "payment", "medicare", "medicaid"],
}


# ---------------------------------------------------------------------------
# 取值同义词 -> 维度取值的标准化映射
# 用于把「老年人」「65岁以上」这类口语统一到 SPARCS 标准取值「70 or Older」
# ---------------------------------------------------------------------------
VALUE_SYNONYMS: dict[str, dict[str, list[str]]] = {
    "age_group": {
        "0 to 17":       ["儿童", "小孩", "未成年", "婴幼儿", "婴儿", "pediatric", "child", "kid"],
        "18 to 29":      ["青年", "年轻人", "18-29", "young"],
        "30 to 49":      ["中年", "中年人", "30-49", "middle"],
        "50 to 69":      ["中老年", "50-69", "50-69岁"],
        "70 or Older":   ["老年", "老年人", "高龄", "70以上", "70+", "elderly", "old"],
    },
    "gender": {
        "Male":          ["男", "男性", "male", "m"],
        "Female":        ["女", "女性", "female", "f"],
        "Unknown":       ["未知", "不详", "unknown"],
    },
    "type_of_admission": {
        "Emergency":      ["急诊", "急诊入院", "emergency"],
        "Elective":       ["择期", "择期入院", "elective"],
        "Urgent":         ["紧急", "urgent"],
        "Newborn":        ["新生儿", "newborn"],
        "Trauma":         ["外伤", "创伤", "trauma"],
    },
    "apr_severity_of_illness_description": {
        "Minor":          ["轻微", "轻症", "minor"],
        "Moderate":       ["中度", "中等", "moderate"],
        "Major":          ["重度", "major"],
        "Extreme":        ["极重", "危重", "extreme", "critical"],
    },
    "apr_risk_of_mortality": {
        "Minor":          ["死亡风险低", "minor"],
        "Moderate":       ["死亡风险中", "moderate"],
        "Major":          ["死亡风险高", "major"],
        "Extreme":        ["死亡风险极高", "extreme"],
    },
    "apr_medical_surgical_description": {
        "Medical":        ["内科", "medical"],
        "Surgical":       ["外科", "surgical", "手术"],
    },
    "payment_typology_1": {
        "Medicare":               ["medicare", "联邦医保"],
        "Medicaid":               ["medicaid", "州医保"],
        "Private Health Insurance": ["商业保险", "私人保险", "private"],
        "Self-Pay":               ["自费", "self-pay"],
        "Department of Corrections": ["矫正部门", "corrections"],
        "Government":             ["政府", "government"],
    },
    "emergency_department_indicator": {
        "Y":               ["急诊", "是急诊", "ed就诊", "y"],
        "N":               ["非急诊", "n"],
    },
}

# ---------------------------------------------------------------------------
# 指标关键词：哪些词暗示用户想要哪个聚合指标
# ---------------------------------------------------------------------------
METRIC_KEYWORDS: dict[str, list[str]] = {
    "discharge_count":             ["人次", "人数", "多少", "几条", "count"],
    "avg_length_of_stay":          ["平均住院", "平均住院时长", "平均住院天数", "住院时长"],
    "max_length_of_stay":          ["最长住院", "最长住院时长"],
    "avg_total_charges":           ["平均费用", "平均花费", "平均总费用"],
    "sum_total_charges":           ["总费用", "总花费", "费用合计", "费用总和"],
    "avg_total_costs":             ["平均成本", "平均总成本"],
    "sum_total_costs":             ["总成本", "成本合计"],
    "avg_birth_weight":            ["平均出生体重", "出生体重"],
    "avg_severity_of_illness":     ["平均严重度", "平均病情"],
}

# ---------------------------------------------------------------------------
# 算法关键词：哪些词暗示用户想做哪个复杂算法（驱动意图分类）
# 注意：避免与聚合维度关键词冲突——"风险" 单独太宽泛，必须用更具体词
# ---------------------------------------------------------------------------
ALGORITHM_KEYWORDS: dict[str, list[str]] = {
    "statistics":          ["整体", "总览", "概览", "概要", "总体", "全平台", "全部", "总统计",
                            "整体情况", "总体情况", "数据总览", "整体统计"],
    "association":         ["关联", "伴生", "伴随", "经常一起", "同时出现", "common",
                            "associated", "经常和", "经常伴随", "和...一起"],
    "cost_prediction":     ["预测费用", "预测花费", "费用预测", "预估费用", "预估花费",
                            "花费预测", "predict", "预测住院费用", "预估住院费用",
                            "预测住院花费", "预估住院花费"],
    "readmission_risk":    ["再入院", "再住院", "readmission", "再入院风险", "再住院风险"],
}

# ---------------------------------------------------------------------------
# 元数据查询关键词（清单 / 能力 / 支持什么 / 算法可用 等）
# ---------------------------------------------------------------------------
METADATA_KEYWORDS: list[str] = [
    "哪些维度", "哪些指标", "支持什么", "能做什么", "可用算法",
    "有什么算法", "维度清单", "指标清单", "算法清单",
    "有哪些维度", "有哪些指标", "有哪些算法", "能查询什么", "能干什么",
    "能分析什么", "可用能力", "可用分析能力", "支持哪些", "支持什么分析",
]

# ---------------------------------------------------------------------------
# 反向索引：把同义词列表反查为 同义词 -> (dimension, value) 字典，加速抽取
# ---------------------------------------------------------------------------
SYNONYM_REVERSE: list[tuple[str, str, str]] = []  # (synonym, dimension, value)
for dim, mapping in VALUE_SYNONYMS.items():
    for value, synonyms in mapping.items():
        for s in synonyms:
            SYNONYM_REVERSE.append((s, dim, value))
# 按长度降序排列，优先匹配更长的同义词（避免「男」吃掉「男性」）
SYNONYM_REVERSE.sort(key=lambda x: -len(x[0]))


def normalize_value(dimension: str, raw: str | None) -> str | None:
    """把自然语言取值映射到维度标准取值；不在词典则原样返回。"""
    if raw is None:
        return None
    raw = str(raw).strip()
    synonyms = VALUE_SYNONYMS.get(dimension, {})
    for value, syns in synonyms.items():
        if raw == value or raw in syns:
            return value
    return raw


def find_synonym(text: str) -> tuple[str, str] | None:
    """从输入文本中查找第一个匹配的同义词，返回 (dimension, value)；找不到返回 None。"""
    text_lower = text.lower()
    for synonym, dim, value in SYNONYM_REVERSE:
        if synonym in text or synonym.lower() in text_lower:
            return dim, value
    return None
