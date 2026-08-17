# 数据字典（33 个字段）

说明：
- **清洗后字段名**：列名统一化后的小写下划线名称。
- **类型**：清洗后的最终数据类型。`str`=文本/编码，`int`=可空整数，`float`=浮点。
- **取值/示例**：来自对原始数据的实际探查。
- **清洗规则**：对应 `cleaners.py` 中的处理函数。
- **为什么这样处理**：该字段必须这样处理的根本原因。

| # | 原始字段 | 清洗后字段名 | 类型 | 取值/示例 | 清洗规则 | 为什么这样处理 |
|---|---|---|---|---|---|---|
| 1 | Hospital Service Area | `hospital_service_area` | str | New York City 等 | 去空白+缺失填 Unknown | 文本分类字段，去空白防分裂，统一缺失哨兵 |
| 2 | Hospital County | `hospital_county` | str | Bronx、Kings… | 同上 | 同上 |
| 3 | Operating Certificate Number | `operating_certificate_number` | str | 7 位号，如 `0101000` | 保字符串 | 机构编码，前导零有意义，转数值会丢零 |
| 4 | Permanent Facility Id | `permanent_facility_id` | str | 6 位号，如 `001169` | 保字符串 | 机构标识，前导零有意义 |
| 5 | Facility Name | `facility_name` | str | 医院全称 | 去空白+缺失填 Unknown | 名称字段去空白、统一缺失 |
| 6 | Age Group | `age_group` | str | 0 to 17 / 18 to 29 / 30 to 49 / 50 to 69 / 70 or Older | 去空白+缺失填 Unknown | 5 档年龄段，统一取值避免统计分裂 |
| 7 | Zip Code - 3 digits | `zip_code_3_digits` | str | `104`、`112`… 及 `OOS`(州外) | 保字符串 | 含非数字取值 `OOS`，不能转数值 |
| 8 | Gender | `gender` | str | M/F/U → Male/Female/Unknown | 映射标准化 | 统一成全拼，U 及脏值归 Unknown |
| 9 | Race | `race` | str | White / Other Race / Black/African American / Multi-racial | 去空白+缺失填 Unknown | 种族分类，去空白、统一缺失 |
| 10 | Ethnicity | `ethnicity` | str | Not Span/Hispanic / Spanish/Hispanic / Unknown / Multi-ethnic | 去空白+缺失填 Unknown | 族裔分类，源数据已含 `Unknown` 取值，保留 |
| 11 | Length of Stay | `length_of_stay` | int | 1~120（天） | 转整数 | 住院天数本质是整数，转数值才能做统计 |
| 12 | Type of Admission | `type_of_admission` | str | Emergency / Elective / Newborn / Urgent / Trauma / Not Available | 去空白+缺失填 Unknown | 入院类型；`Not Available` 是源数据合法值，保留 |
| 13 | Patient Disposition | `patient_disposition` | str | Home or Self Care、Expired 等 19 种 | 去空白+缺失填 Unknown | 出院去向，去空白、统一缺失 |
| 14 | Discharge Year | `discharge_year` | int | 2021 | 转整数 | 年份是整数 |
| 15 | CCSR Diagnosis Code | `ccsr_diagnosis_code` | str | `INF012`、`CIR019` 等 | 保字符串 | 疾病编码，字母数字混合，须保字符串 |
| 16 | CCSR Diagnosis Description | `ccsr_diagnosis_description` | str | CORONAVIRUS DISEASE 2019… | 去空白+缺失填 Unknown | 疾病描述文本 |
| 17 | CCSR Procedure Code | `ccsr_procedure_code` | str | `OTR004` 等，约 57.6 万条为空 | 保字符串+缺失填 Unknown | 操作编码；空表示“未记录操作” |
| 18 | CCSR Procedure Description | `ccsr_procedure_description` | str | ISOLATION PROCEDURES… | 去空白+缺失填 Unknown | 操作描述文本 |
| 19 | APR DRG Code | `apr_drg_code` | str | `137`、`045` 等 | 保字符串 | DRG 编码，`045` 前导零有意义 |
| 20 | APR DRG Description | `apr_drg_description` | str | MAJOR RESPIRATORY INFECTIONS… | 去空白+缺失填 Unknown | DRG 描述文本 |
| 21 | APR MDC Code | `apr_mdc_code` | str | `05`、`04`、`14` 等 | 保字符串 | MDC 编码，`05` 前导零有意义 |
| 22 | APR MDC Description | `apr_mdc_description` | str | DISEASES AND DISORDERS OF… | 去空白+缺失填 Unknown | MDC 描述文本 |
| 23 | APR Severity of Illness Code | `apr_severity_of_illness_code` | int | 0~4 | 转整数 | 严重程度代码是序数，转数值便于排序 |
| 24 | APR Severity of Illness Description | `apr_severity_of_illness_description` | str | Minor / Moderate / Major / Extreme | 去空白+缺失填 Unknown | 严重程度描述文本 |
| 25 | APR Risk of Mortality | `apr_risk_of_mortality` | str | Minor / Moderate / Major / Extreme（约 2550 条缺失） | 去空白+缺失填 Unknown | **文本字段**，非数字；旧脚本误当数值处理会整列变 NaN |
| 26 | APR Medical Surgical Description | `apr_medical_surgical_description` | str | Medical / Surgical / Not Applicable | 去空白+缺失填 Unknown | 内科/外科标志描述 |
| 27 | Payment Typology 1 | `payment_typology_1` | str | Medicare / Medicaid / Private… 等 9 种 | 去空白+缺失填 Unknown | 主支付方式，值域已规范 |
| 28 | Payment Typology 2 | `payment_typology_2` | str | 约 107 万条为空（次支付方式） | 去空白+缺失填 Unknown | 次支付方式，多数为空属正常 |
| 29 | Payment Typology 3 | `payment_typology_3` | str | 约 177 万条为空（第三支付方式） | 去空白+缺失填 Unknown | 第三支付方式，多数为空属正常 |
| 30 | Birth Weight | `birth_weight` | int | `03200` → `3200`（克）；约 189 万条为空 | 转整数（空→NaN） | 出生体重（克）；非新生儿记录为空属正常 |
| 31 | Emergency Department Indicator | `emergency_department_indicator` | str | Y / N | 只保留 Y/N，其余 Unknown | 是否急诊，严格二值化，脏值不混入布尔判断 |
| 32 | Total Charges | `total_charges` | float | `"320,922.43"` → `320922.43` | 去逗号转浮点 | 金额带千分位逗号且为字符串，转数值才能求和/建模 |
| 33 | Total Costs | `total_costs` | float | `"60,241.34"` → `60241.34` | 去逗号转浮点 | 同上 |

---

## 字段分组速查（对应 cleaning_config.py）

| 分组 | 字段 | 处理函数 |
|---|---|---|
| `TEXT_COLUMNS`（15） | 描述/分类文本 | `clean_text`：去空白 + 缺失填 Unknown |
| `CODE_COLUMNS`（7） | 编码/标识 | `clean_text`：保字符串原样 + 缺失填 Unknown |
| `PAYMENT_COLUMNS`（3） | 支付方式 | `clean_text`：去空白 + 缺失填 Unknown |
| `INT_COLUMNS`（4） | 整型 | `to_nullable_int`：去逗号转 Int64，失败置 NaN |
| `MONEY_COLUMNS`（2） | 金额 | `to_money`：去逗号/非数字转 float |
| 特殊映射（2） | `gender`、`emergency_department_indicator` | `standardize_gender` / `standardize_yn` |
