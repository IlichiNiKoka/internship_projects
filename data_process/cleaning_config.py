"""集中配置：路径、常量、字段分类与映射表。

为什么单独抽出一个 config.py？
------------------------------------------------------------------
把所有“可能随环境变化、或调整清洗规则时需要改动”的魔法值集中在一处，
后续维护时只需要改这里，不用在业务代码里逐行翻找。例如：
  * 换机器后路径变了         -> 改 RAW_DIR / ARCHIVE_PATH
  * 内存紧张想把分块调小     -> 改 CHUNK_SIZE
  * 想统一用别的缺失哨兵     -> 改 UNKNOWN
  * 想调整某字段的取值映射   -> 改 GENDER_MAP 等映射表

字段按“清洗方式”分成五组（见下方 *_COLUMNS），每组对应 cleaners.py 里的
一个处理函数。这样新增/删除字段时，只需在对应列表里增删一行即可。
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# 路径（基于本文件位置推导，不写死盘符，方便迁移）
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent      # .../医养项目数据处理/
DATA_ROOT = PROJECT_ROOT.parent                     # .../大数据实习/
RAW_DIR = PROJECT_ROOT / "raw"                      # 已解压的原始 CSV 目录
OUTPUT_DIR = PROJECT_ROOT / "processed"             # 清洗结果输出目录

# 输出文件
CLEAN_CSV = OUTPUT_DIR / "Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012_clean.csv"
REPORT_PATH = OUTPUT_DIR / "processing_report.json"

# 原始 RAR 归档（仅当 raw 目录下找不到 CSV 时才用它兜底解压）
ARCHIVE_PATH = DATA_ROOT / "009 医养项目数据" / "Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv.rar"
# 7z 可执行文件路径（仅兜底解压时使用；本项目数据已解压，通常用不到）
SEVEN_ZIP = r"C:\Program Files\bililive\livehime\7.54.0.10521\7z.exe"

# ---------------------------------------------------------------------------
# 读取参数
# ---------------------------------------------------------------------------
CHUNK_SIZE = 200_000        # 分块行数：2,101,588 行的大文件分块读取，避免整表载入内存
SOURCE_ENCODING = "utf-8"   # 源文件编码

# ---------------------------------------------------------------------------
# 缺失值哨兵
# ---------------------------------------------------------------------------
# 分类/文本字段的缺失统一填充为 "Unknown"（人可读、可 grep、不参与数值统计）。
# 数值字段的缺失则用 NaN（真正的“无值”），两者语义不同，故分开处理。
UNKNOWN = "Unknown"

# ---------------------------------------------------------------------------
# 字段分类（均为“列名统一化之后”的名称，见 data_dictionary.md）
# ---------------------------------------------------------------------------
# 描述/分类文本字段：去空白，缺失填 Unknown
TEXT_COLUMNS = [
    "hospital_service_area",
    "hospital_county",
    "facility_name",
    "age_group",
    "race",
    "ethnicity",
    "type_of_admission",
    "patient_disposition",
    "ccsr_diagnosis_description",
    "ccsr_procedure_description",
    "apr_drg_description",
    "apr_mdc_description",
    "apr_severity_of_illness_description",
    "apr_risk_of_mortality",          # 注意：这是文本(Minor/Moderate/...)，不是数字
    "apr_medical_surgical_description",
]

# 编码/标识类字段：保留原样字符串（含前导零），仅去空白，缺失填 Unknown
CODE_COLUMNS = [
    "operating_certificate_number",   # 7 位运营证书号，如 "0101000"
    "permanent_facility_id",          # 6 位机构号，含前导零，如 "001169"
    "zip_code_3_digits",              # 3 位邮编前缀，含 "OOS"(州外)，不是纯数字
    "ccsr_diagnosis_code",            # 如 "INF012"
    "ccsr_procedure_code",            # 如 "OTR004"，可为空
    "apr_drg_code",                   # 3 位 DRG 码，含前导零，如 "045"
    "apr_mdc_code",                   # 2 位 MDC 码，含前导零，如 "05"
]

# 支付方式字段：值域本身已规范，仅需去空白 + 缺失填 Unknown
PAYMENT_COLUMNS = [
    "payment_typology_1",
    "payment_typology_2",
    "payment_typology_3",
]

# 整型字段：去逗号 -> 数值 -> 缺失为 NaN
INT_COLUMNS = [
    "length_of_stay",                 # 住院天数（天）
    "discharge_year",                 # 出院年份
    "apr_severity_of_illness_code",   # 病情严重程度代码 0~4
    "birth_weight",                   # 出生体重（克），如 "03200" -> 3200
]

# 金额字段：去逗号与非数字字符 -> 浮点数
MONEY_COLUMNS = [
    "total_charges",                  # 总费用
    "total_costs",                    # 总成本
]

# ---------------------------------------------------------------------------
# 取值映射表（用于标准化个别字段，见 cleaners.py）
# ---------------------------------------------------------------------------
# 性别：数据中实际为 M/F/U，另兼容 Male/Female/Unknown 等全拼写法
GENDER_MAP = {
    "m": "Male",
    "f": "Female",
    "u": "Unknown",
    "male": "Male",
    "female": "Female",
    "unknown": "Unknown",
}

# 急诊标志：只认 Y/N，其余一律视为 Unknown（避免脏值混入布尔判断）
VALID_YN = {"Y", "N"}

# 报告里需要输出“取值分布”的关键字段，用于人工核验清洗效果
REPORT_DISTRIBUTION_FIELDS = [
    "gender",
    "payment_typology_1",
    "age_group",
    "type_of_admission",
    "emergency_department_indicator",
]
