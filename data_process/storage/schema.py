# -*- coding: utf-8 -*-
"""表结构与索引定义（人员2 · 数据库设计）。

设计要点
--------
  1. **字段与清洗后数据字典一一对应**：33 个业务字段的类型与 `data_dictionary.md`
     保持一致（27 字符串 / 4 整型 / 2 浮点），清洗后 CSV 是唯一数据源；
  2. **自增主键 + 行哈希唯一键**：医疗记录没有天然主键（同一患者可多次住院），
     因此引入 `id` 自增主键方便定位，`row_hash` 唯一键用于二期增量更新
     （UC7：同一行重复入库时按唯一键跳过，避免重复导入）；
  3. **索引面向下游分析维度**：按人员3 多维度聚合的高频维度（出院年份、
     县、疾病编码、年龄组、性别、主支付方式）建普通索引，加速 GROUP BY 查询；
  4. **双方言 DDL**：MySQL 用 VARCHAR/INT/DOUBLE 显式长度，SQLite 退化为
     TEXT/INTEGER/REAL（SQLite 无 VARCHAR 长度约束），保证兜底可用。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 字段分组（顺序 = 清洗后 CSV 的规范列顺序，见 data_dictionary.md）
# ---------------------------------------------------------------------------
# 27 个字符串字段
STRING_FIELDS = [
    "hospital_service_area", "hospital_county", "operating_certificate_number",
    "permanent_facility_id", "facility_name", "age_group", "zip_code_3_digits",
    "gender", "race", "ethnicity", "type_of_admission", "patient_disposition",
    "ccsr_diagnosis_code", "ccsr_diagnosis_description", "ccsr_procedure_code",
    "ccsr_procedure_description", "apr_drg_code", "apr_drg_description",
    "apr_mdc_code", "apr_mdc_description", "apr_severity_of_illness_description",
    "apr_risk_of_mortality", "apr_medical_surgical_description",
    "payment_typology_1", "payment_typology_2", "payment_typology_3",
    "emergency_department_indicator",
]

# 4 个整型字段
INT_FIELDS = [
    "length_of_stay", "discharge_year",
    "apr_severity_of_illness_code", "birth_weight",
]

# 2 个浮点字段
DOUBLE_FIELDS = ["total_charges", "total_costs"]

# 33 个业务字段的规范顺序（27 + 4 + 2）
ORDERED_FIELDS = STRING_FIELDS + INT_FIELDS + DOUBLE_FIELDS

# 入库后附加的两个系统列
ROW_HASH_COLUMN = "row_hash"   # 整行内容哈希，唯一键（增量更新去重依据）
ID_COLUMN = "id"               # 自增主键


@dataclass(frozen=True)
class ColumnSpec:
    """单列规格：字段名 + MySQL 类型 + 中文注释。"""

    name: str
    mysql_type: str
    comment: str = ""


# ---------------------------------------------------------------------------
# 33 个业务字段的 MySQL 类型与注释（长度按实际取值规模取宽松值，避免截断）
# ---------------------------------------------------------------------------
FIELD_SPECS: list[ColumnSpec] = [
    # ---- 字符串字段 ----
    ColumnSpec("hospital_service_area", "VARCHAR(64)", "医院服务区域"),
    ColumnSpec("hospital_county", "VARCHAR(64)", "所在县"),
    ColumnSpec("operating_certificate_number", "VARCHAR(16)", "运营证书号（保前导零）"),
    ColumnSpec("permanent_facility_id", "VARCHAR(16)", "机构永久标识（保前导零）"),
    ColumnSpec("facility_name", "VARCHAR(255)", "医院全称"),
    ColumnSpec("age_group", "VARCHAR(32)", "年龄组"),
    ColumnSpec("zip_code_3_digits", "VARCHAR(8)", "邮编前缀（含 OOS 州外）"),
    ColumnSpec("gender", "VARCHAR(16)", "性别 Male/Female/Unknown"),
    ColumnSpec("race", "VARCHAR(64)", "种族"),
    ColumnSpec("ethnicity", "VARCHAR(64)", "族裔"),
    ColumnSpec("type_of_admission", "VARCHAR(32)", "入院类型"),
    ColumnSpec("patient_disposition", "VARCHAR(128)", "出院去向"),
    ColumnSpec("ccsr_diagnosis_code", "VARCHAR(16)", "CCSR 诊断编码"),
    ColumnSpec("ccsr_diagnosis_description", "VARCHAR(255)", "CCSR 诊断描述"),
    ColumnSpec("ccsr_procedure_code", "VARCHAR(16)", "CCSR 操作编码"),
    ColumnSpec("ccsr_procedure_description", "VARCHAR(255)", "CCSR 操作描述"),
    ColumnSpec("apr_drg_code", "VARCHAR(16)", "APR DRG 编码（保前导零）"),
    ColumnSpec("apr_drg_description", "VARCHAR(255)", "APR DRG 描述"),
    ColumnSpec("apr_mdc_code", "VARCHAR(16)", "APR MDC 编码（保前导零）"),
    ColumnSpec("apr_mdc_description", "VARCHAR(255)", "APR MDC 描述"),
    ColumnSpec("apr_severity_of_illness_description", "VARCHAR(32)", "病情严重程度描述"),
    ColumnSpec("apr_risk_of_mortality", "VARCHAR(32)", "死亡风险（文本字段）"),
    ColumnSpec("apr_medical_surgical_description", "VARCHAR(32)", "内外科标志"),
    ColumnSpec("payment_typology_1", "VARCHAR(64)", "支付方式(主)"),
    ColumnSpec("payment_typology_2", "VARCHAR(64)", "支付方式(次)"),
    ColumnSpec("payment_typology_3", "VARCHAR(64)", "支付方式(三)"),
    ColumnSpec("emergency_department_indicator", "VARCHAR(8)", "急诊标志 Y/N"),
    # ---- 整型字段 ----
    ColumnSpec("length_of_stay", "INT", "住院天数"),
    ColumnSpec("discharge_year", "SMALLINT", "出院年份"),
    ColumnSpec("apr_severity_of_illness_code", "TINYINT", "病情严重程度代码 0~4"),
    ColumnSpec("birth_weight", "INT", "出生体重（克）"),
    # ---- 浮点字段 ----
    ColumnSpec("total_charges", "DOUBLE", "总费用（美元）"),
    ColumnSpec("total_costs", "DOUBLE", "总成本（美元）"),
]

FIELD_SPEC_BY_NAME: dict[str, ColumnSpec] = {s.name: s for s in FIELD_SPECS}

# 面向人员3 聚合分析高频维度的普通索引（MySQL / SQLite 均可使用）
INDEX_COLUMNS: list[str] = [
    "discharge_year",
    "hospital_county",
    "ccsr_diagnosis_code",
    "age_group",
    "gender",
    "payment_typology_1",
]


def _sqlite_type(mysql_type: str) -> str:
    """把 MySQL 类型退化为 SQLite 的宽松类型（SQLite 无长度约束）。"""
    upper = mysql_type.upper()
    if upper.startswith("VARCHAR"):
        return "TEXT"
    if upper in {"DOUBLE", "FLOAT"}:
        return "REAL"
    if upper in {"INT", "SMALLINT", "TINYINT", "BIGINT", "INTEGER"}:
        return "INTEGER"
    return "TEXT"  # 兜底


def mysql_ddl(table: str) -> str:
    """生成 MySQL 建表语句（含主键、唯一键、普通索引）。

    为什么用 CREATE TABLE IF NOT EXISTS：
      重复执行入库脚本不应因表已存在而报错，便于重复部署与幂等。
    """
    lines = [f"CREATE TABLE IF NOT EXISTS `{table}` ("]
    lines.append(f"  `{ID_COLUMN}` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',")
    for spec in FIELD_SPECS:
        lines.append(f"  `{spec.name}` {spec.mysql_type} NULL COMMENT '{spec.comment}',")
    lines.append(f"  `{ROW_HASH_COLUMN}` CHAR(20) NOT NULL COMMENT '整行内容哈希（唯一键）',")
    lines.append(f"  PRIMARY KEY (`{ID_COLUMN}`),")
    lines.append(f"  UNIQUE KEY `uk_row_hash` (`{ROW_HASH_COLUMN}`),")
    for i, col in enumerate(INDEX_COLUMNS):
        comma = "," if i < len(INDEX_COLUMNS) - 1 else ""
        lines.append(f"  KEY `idx_{col}` (`{col}`){comma}")
    lines.append(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='SPARCS 2021 清洗后住院数据';")
    return "\n".join(lines)


def sqlite_ddl(table: str) -> str:
    """生成 SQLite 建表语句（类型退化 + 唯一键 + 普通索引）。"""
    lines = [f"CREATE TABLE IF NOT EXISTS {table} ("]
    lines.append(f"  {ID_COLUMN} INTEGER PRIMARY KEY AUTOINCREMENT,")
    for spec in FIELD_SPECS:
        lines.append(f"  {spec.name} {_sqlite_type(spec.mysql_type)},")
    lines.append(f"  {ROW_HASH_COLUMN} TEXT NOT NULL UNIQUE")
    lines.append(");")
    for col in INDEX_COLUMNS:
        lines.append(
            f"CREATE INDEX IF NOT EXISTS idx_{col} ON {table}({col});"
        )
    return "\n".join(lines)
