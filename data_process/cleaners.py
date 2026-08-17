"""清洗函数库：一组“纯函数”，负责把原始字段转成标准形态。

每个函数都遵循同一个约定：
  * 输入：pd.Series（原始字符串列，dtype=object）
  * 输出：pd.Series（清洗后，类型统一）
  * 不依赖全局状态，不做 IO —— 便于单元测试和复用

所有“为什么要这样处理”的解释都写在函数 docstring 里。
"""

from __future__ import annotations

import re

import pandas as pd

import cleaning_config as config


# ---------------------------------------------------------------------------
# 列名统一化
# ---------------------------------------------------------------------------
def normalize_column_name(name: str) -> str:
    """把原始列名转成统一的 snake_case。

    为什么：
      原始列名混杂了空格、连字符、括号、斜杠（如 "Total Charges"、
      "Zip Code - 3 digits"、"APR MDC Code"），在代码里引用、在数据库建表、
      在绘图时都会带来麻烦。统一成小写下划线命名后，全链路字段名一致、
      不会再因大小写/空格不一致而出错。

    怎么做：
      1) 把连续空白折叠成单个空格并去掉首尾空白；
      2) 把 - / ( ) 这些分隔符替换成空格；
      3) 再把空白替换成下划线、整体转小写。
    """
    cleaned = re.sub(r"\s+", " ", str(name)).strip()
    cleaned = cleaned.replace("-", " ").replace("/", " ")
    cleaned = cleaned.replace("(", " ").replace(")", " ")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned.lower()


# ---------------------------------------------------------------------------
# 文本/分类字段：去空白 + 统一缺失哨兵
# ---------------------------------------------------------------------------
# 常见“假缺失”占位符：这些字符串本质上表示“无值”，应统一成 UNKNOWN
_PLACEHOLDER_TOKENS = {"", "nan", "none", "null", "na", "n/a", "<na>"}


def clean_text(series: pd.Series) -> pd.Series:
    """文本字段通用清洗：去空白、折叠空白、缺失统一为 "Unknown"。

    为什么：
      1) 医疗数据在手工录入/不同系统导出时，常在值前后带空格或 NBSP 空格，
         导致 "Male " 和 "Male" 被当成两个不同的取值，统计会分裂。
      2) 缺失值可能以空字符串、"nan"、"None"、"null" 等多种形态出现，
         若不统一，下游在判断“是否缺失”时容易漏判。
      3) 用统一的 "Unknown" 哨兵，比空字符串更显式，方便 grep、过滤和可视化。

    怎么做：
      1) 先折叠内部连续空白（含 NBSP  ）并去掉首尾空白；
      2) 若结果为空或是常见占位符，统一返回 "Unknown"；
      3) 其余返回清洗后的字符串。
    """

    def _clean(v):
        if pd.isna(v):                     # NaN/None 属于缺失
            return config.UNKNOWN
        s = re.sub(r"\s+", " ", str(v).replace(" ", " ")).strip()
        if s.lower() in _PLACEHOLDER_TOKENS:
            return config.UNKNOWN
        return s

    return series.map(_clean)


# ---------------------------------------------------------------------------
# 性别标准化
# ---------------------------------------------------------------------------
def standardize_gender(series: pd.Series) -> pd.Series:
    """把性别统一成 Male / Female / Unknown。

    为什么：
      原始值虽然是 M/F/U 这种缩写，但为了后续分析、报表和模型的可读性，
      统一成全拼更直观；同时把 U（及任何脏值）统一成 "Unknown"，
      避免出现 "M"/"m"/"Male" 并存导致的统计分裂。

    怎么做：
      取小写后查 GENDER_MAP，命中则映射；未命中/缺失一律 "Unknown"。
    """

    def _map(v):
        if pd.isna(v):
            return config.UNKNOWN
        return config.GENDER_MAP.get(str(v).strip().lower(), config.UNKNOWN)

    return series.map(_map)


# ---------------------------------------------------------------------------
# 是/否标志标准化
# ---------------------------------------------------------------------------
def standardize_yn(series: pd.Series) -> pd.Series:
    """把是/否标志统一成 Y / N / Unknown。

    为什么：
      Emergency Department Indicator 这种二值标志，如果混入大小写不一
      或空值，下游做布尔过滤（是否急诊）时会漏算。只认 Y/N、其余归 Unknown，
      能保证“是否急诊”这个口径严格二值。

    怎么做：
      取大写后只保留 Y/N，其余（含缺失）统一 "Unknown"。
    """

    def _map(v):
        if pd.isna(v):
            return config.UNKNOWN
        s = str(v).strip().upper()
        return s if s in config.VALID_YN else config.UNKNOWN

    return series.map(_map)


# ---------------------------------------------------------------------------
# 整型字段
# ---------------------------------------------------------------------------
def to_nullable_int(series: pd.Series) -> pd.Series:
    """把整型字段清洗为可空的 Int64。

    为什么：
      住院天数、出院年份、严重程度代码、出生体重等字段本质是整数，
      原始却以字符串读入（如出生体重 "03200"）。转成数值后才能做大小比较、
      求和、均值等统计。用 pandas 的 Int64 可空整型，既保留整数语义，
      又能表达缺失值（NaN），而不会像普通 int 那样把缺失逼成 0 或报错。

    怎么做：
      1) 去空白、去千分位逗号；
      2) 转数值，失败者为 NaN；
      3) 只保留整数值（非整数视为异常 -> NaN，避免静默四舍五入）；
      4) 四舍五入后转 Int64。
    """
    s = series.astype(str).str.strip().str.replace(",", "", regex=False)
    numeric = pd.to_numeric(s, errors="coerce")
    numeric = numeric.where(numeric.isna() | (numeric == numeric.round()))
    return numeric.round().astype("Int64")


# ---------------------------------------------------------------------------
# 金额字段
# ---------------------------------------------------------------------------
def to_money(series: pd.Series) -> pd.Series:
    """把金额字符串清洗为浮点数。

    为什么：
      金额字段原始形如 "320,922.43"，带千分位逗号，且整体是字符串。
      若不处理，后续 sum()/mean() 或模型训练都会因“字符串不能算数”而失败。
      去掉逗号、只保留数字和小数点（及负号），再转 float 即可。

    怎么做：
      1) 去空白、去逗号；
      2) 用正则剔除非数字/小数点/负号字符（防御性，防止混入货币符号等）；
      3) 转 float，失败者为 NaN。
    """
    s = series.astype(str).str.strip().str.replace(",", "", regex=False)
    s = s.str.replace(r"[^0-9.\-]", "", regex=True)
    return pd.to_numeric(s, errors="coerce")


# ---------------------------------------------------------------------------
# 分块清洗入口
# ---------------------------------------------------------------------------
def clean_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """对一个原始数据块执行完整清洗，返回清洗后的 DataFrame。

    为什么这样组织：
      把“每一步怎么做”拆成上面的小函数，这里只负责按字段分类把函数套上去，
      逻辑清晰、便于审查。清洗不涉及跨块状态，因此去重放到 pipeline 里做
      （去重需要跨块共享“已见过哪些行”的信息）。
    """
    df = chunk.copy()
    df.columns = [normalize_column_name(c) for c in df.columns]

    # 1) 文本/描述类：去空白 + 缺失填 Unknown
    for col in config.TEXT_COLUMNS:
        if col in df.columns:
            df[col] = clean_text(df[col])

    # 2) 编码/标识类：保留字符串原样（前导零不丢），缺失填 Unknown
    for col in config.CODE_COLUMNS:
        if col in df.columns:
            df[col] = clean_text(df[col])

    # 3) 支付方式：值域已规范，仅去空白 + 缺失填 Unknown
    for col in config.PAYMENT_COLUMNS:
        if col in df.columns:
            df[col] = clean_text(df[col])

    # 4) 整型字段
    for col in config.INT_COLUMNS:
        if col in df.columns:
            df[col] = to_nullable_int(df[col])

    # 5) 金额字段
    for col in config.MONEY_COLUMNS:
        if col in df.columns:
            df[col] = to_money(df[col])

    # 6) 特殊映射字段
    if "gender" in df.columns:
        df["gender"] = standardize_gender(df["gender"])
    if "emergency_department_indicator" in df.columns:
        df["emergency_department_indicator"] = standardize_yn(df["emergency_department_indicator"])

    return df
