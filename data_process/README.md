# 医养项目数据处理与分析

本目录包含两个相互衔接的子系统：

1. **数据清洗流水线**（根目录脚本）：对 SPARCS 2021 住院出院数据
   （约 832 MB / 2,101,588 行）进行标准化清洗，产出统一字段类型、统一取值域、
   去重后的高质量 CSV，作为下游分析服务的唯一数据源。
2. **大数据分析服务**（`app/` 子目录）：基于 Flask + PySpark 的 REST 服务，
   对清洗后数据提供多维度聚合、统计指标、关联分析、住院费用预测、再入院风险评估等接口，
   供上层 AI 智能层（人员4）调用。

> 需求来源：`processing_reason.txt`（清洗）与 `docs/markdown/需求文档.md`（分析服务）。

---

## 1. 数据来源

| 项 | 内容 |
|---|---|
| 数据文件 | `Hospital_Inpatient_Discharges__SPARCS_De-Identified___2021_20231012.csv` |
| 来源 | 纽约州 SPARCS（Statewide Planning and Research Cooperative System）去标识化住院出院数据 |
| 原始大小 | 约 832 MB（解压后） |
| 行数 | 2,101,588 |
| 字段数 | 33 |

原始数据已解压在 `raw/` 目录下（`raw/<同名目录>/<同名>.csv` 的嵌套结构，
是 7z“解压到同名文件夹”造成的，脚本用递归搜索自动适配，无需手动调整）。

---

## 2. 目录结构

```
data_process/
├── README.md                # 本文件：项目说明
├── requirements.txt         # 统一依赖清单（数据清洗 + 分析服务）
├── .env.example             # 分析服务环境变量示例
├── pytest.ini               # 测试配置
├── run.py                   # 分析服务开发入口（python run.py）
├── wsgi.py                  # 分析服务生产入口（gunicorn wsgi:app）
│
├── # ---- 数据清洗流水线 ----
├── cleaning_config.py       # 清洗配置：路径、分块大小、字段分类、映射表
├── cleaners.py              # 清洗函数库（纯函数，含“为什么”注释）
├── medical_data_pipeline.py # 主流水线：定位→分块读取→清洗→去重→写出→报告
├── run_pipeline.py          # 清洗入口（python run_pipeline.py）
├── data_dictionary.md       # 33 个字段的数据字典
├── processing_reason.txt    # 清洗需求说明
│
├── # ---- 大数据分析服务 ----
├── app/                     # 应用主包（API + 服务层 + 算法 + 数据层 + 核心层）
├── config/                  # 分析服务配置（settings.py + registry.py）
├── scripts/                 # 工具脚本（冒烟测试等）
├── tests/                   # pytest 测试套件
│
├── # ---- 数据目录 ----
├── raw/                     # 原始数据（已解压）
└── processed/               # 输出：清洗结果 + 分析服务数据源
    ├── *_clean.csv
    └── processing_report.json
```

> 数据清洗与分析服务同处一个项目，共享 `processed/` 目录：
> 流水线产出 `*_clean.csv`，分析服务通过 `DEFAULT_CLEAN_CSV` 自动读取该文件。

---

## 3. 环境与依赖

- Python ≥ 3.12
- 依赖：pandas、Flask、marshmallow、python-dotenv、pyspark、pytest
- Java 8 / 11 / 17（Spark 3.5.1 要求，推荐 17）

安装依赖（建议在虚拟环境中）：

```bash
# Linux/macOS
python3.12 -m venv .venv && source .venv/bin/activate
# Windows
py -3.12 -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
```

> 本项目不内置虚拟环境，便于在不同机器（Windows/Linux）间迁移。
> Java 安装：`apt install openjdk-17-jdk`（Linux）/ `brew install openjdk@17`（macOS）

---

## 4. 如何运行

### 4.1 数据清洗流水线

```bash
python run_pipeline.py
```

运行结束后在 `processed/` 下生成：

| 文件 | 说明 |
|---|---|
| `*_clean.csv` | 清洗后的数据（约 2.1M 行） |
| `processing_report.json` | 处理报告：原始/清洗行数、去重数、各字段缺失量、关键字段分布、清洗规则摘要 |

### 4.2 大数据分析服务

```bash
# 开发环境
python run.py

# 生产环境（Linux + gunicorn）
gunicorn -w 2 -b 0.0.0.0:5000 wsgi:app --timeout 120
```

服务默认监听 `http://127.0.0.1:5000`，主要接口：

- `GET  /api/v1/health` — 服务健康检查
- `GET  /api/v1/meta/dimensions` — 22 个可用维度
- `GET  /api/v1/meta/metrics` — 9 个聚合指标
- `GET  /api/v1/meta/algorithms` — 已注册算法
- `GET  /api/v1/meta/cache` — 缓存运行状态
- `GET  /api/v1/meta/performance` — 接口性能监控（二期：耗时/慢查询/错误分布）
- `POST /api/v1/aggregations/run` — 多维度聚合（二期支持 `page`/`page_size` 分页）
- `POST /api/v1/algorithms/<name>/run` — 算法执行（statistics/association/cost_prediction/readmission_risk 等）

### 4.3 测试

```bash
pytest                  # 全部用例
pytest -m "not spark"   # 跳过依赖 Spark 的用例
```

### 4.4 二期功能（3.3.4 API 性能优化 / 3.3.5 API 异常处理机制）

| 能力 | 实现 | 说明 |
|---|---|---|
| Redis 缓存 | `app/core/cache.py` `RedisCacheBackend` | `ANALYTICS_CACHE_BACKEND=redis` 切换；未安装 redis 包或连接失败时自动降级进程内缓存 |
| 超时控制 | `app/core/timeout.py` | 聚合/算法计算在独立线程限时执行，超时抛 504 并丢弃结果 |
| 大结果集分页 | 聚合请求新增 `page`/`page_size` | 响应含 `pagination`（page/page_size/returned/has_more），翻页口径参与缓存键 |
| 慢查询告警 | `app/core/monitor.py` + 中间件 | 耗时超过 `ANALYTICS_SLOW_QUERY_THRESHOLD_SECONDS` 记入日志告警并汇总监控 |
| Spark 任务参数优化 | `app/utils/spark.py` | 默认开启 Spark SQL AQE 自适应执行（可配置关闭） |
| 权限控制 | 中间件统一守卫 | `ANALYTICS_API_AUTH_ENABLED=true` 后校验 `Authorization: Bearer <token>` / `X-API-Key`，缺 -> 401、错 -> 403 |
| 限流 | `app/core/ratelimiter.py` | 滑动窗口按 Token/IP 计数，超限 -> 429 并回写 `Retry-After` 头 |
| 请求体限制修复 | `app/core/error_codes.py` | 补充 413 错误码，超大请求体返回标准化 413 而非 500 |
| 接口耗时监控 | `GET /api/v1/meta/performance` | 请求量、平均/最大耗时、慢查询明细、错误码分布、缓存状态 |

### 4.5 数据入库与质量保障（人员2 二期：MySQL 入库 / 质量评估 / 备份 / 增量更新）

数据库连接统一由 `config/settings.py` 的 `db_*` 字段控制（`.env` 或 `ANALYTICS_DB_*` 环境变量可覆盖）。

| 能力 | 入口 | 说明 |
|---|---|---|
| 结构化入库（UC4 二期） | `python load_to_db.py` | 清洗后 CSV 写入 MySQL（失败降级 SQLite），按 `row_hash` 唯一键去重 |
| 数据质量评估（UC5） | `python quality_report.py` | 完整性 / 准确性 / 一致性 / 时效性四维评分，产出 `processed/quality_report.json` + `quality_report.html`（ECharts） |
| 备份与恢复（UC6） | `python backup.py` | 手动备份 / `--restore` 恢复 / `--schedule` 定时备份（mysqldump） |
| 增量更新（UC7） | 入库报告 `incremental` 字段 | 记录本次新增 `new_rows_inserted` 与重复跳过 `duplicate_rows_skipped` |

备份文件默认输出到 `data_process/backups/`（已加入 .gitignore）；mysqldump 不在 PATH 时可用 `--bin-dir` 或 `ANALYTICS_MYSQL_BIN_DIR` 指定 MySQL bin 目录。

---

## 5. 清洗规则总览（做了什么 + 为什么）

| # | 步骤 | 怎么做 | 为什么 |
|---|---|---|---|
| 1 | 列名统一 | `"Total Charges"` → `total_charges`，`"Zip Code - 3 digits"` → `zip_code_3_digits` | 原始列名含空格/连字符/括号，统一成小写下划线后全链路字段名一致，避免引用出错 |
| 2 | 文本去空白 | 折叠内部空白（含 NBSP）并去首尾空白 | `"Male "` 与 `"Male"` 会被当成两个值，导致统计分裂 |
| 3 | 缺失统一 | 文本/分类字段缺失 → `Unknown`；数值字段缺失 → `NaN` | 缺失以空串/`nan`/`None` 多种形态出现，统一哨兵便于过滤与统计；文本与数值语义不同，故分别用 `Unknown` 与 `NaN` |
| 4 | 编码保前导零 | 机构号、邮编、DRG/MDC/CCSR 码保留字符串原样 | 这些是**编码/标识**而非测量值，`"045"` 的前导零、`"001169"` 的机构号有意义，转数值会丢失 |
| 5 | 整数转数值 | 住院天数、出院年份、严重程度代码、出生体重转整数 | 转成数值后才能做大小比较、求和、均值；出生体重 `"03200"` → `3200`（克） |
| 6 | 金额转数值 | `"320,922.43"` → `320922.43`（浮点） | 金额带千分位逗号且是字符串，不转数值无法求和/建模 |
| 7 | 性别标准化 | `M/F/U` → `Male/Female/Unknown` | 统一取值域，避免缩写混用导致统计分裂 |
| 8 | 急诊标志标准化 | 只保留 `Y/N`，其余 → `Unknown` | 保证“是否急诊”严格二值，脏值不混入布尔判断 |
| 9 | 跨分块去重 | 用整行内容哈希做流式全局去重 | 去掉完全重复的住院记录，避免重复统计；跨块去重比单块内更彻底 |

> 每个字段的详细规则见 [data_dictionary.md](data_dictionary.md)，
> 每段代码的“为什么”见 `cleaners.py` / `medical_data_pipeline.py` 内的 docstring。

---

## 6. 关键设计决策（相对旧脚本的修正）

1. **`APR Risk of Mortality` 是文本字段，不是数字**
   该字段取值为 `Minor / Moderate / Major / Extreme`（死亡风险分级描述）。
   旧脚本把它放进数值转换列表，会导致整列被转成 NaN，丢失关键信息。本次已修正为文本字段处理。

2. **`Zip Code - 3 digits` 不是纯数字**
   字段中含 `OOS`（Out of State，州外）这一合法取值。旧脚本按数值转换会把
   约 5.9 万条 `OOS` 全部变成 NaN。本次修正为字符串字段，保留 `OOS`。

3. **`APR DRG Code` / `APR MDC Code` 保留字符串**
   如 `045`、`05` 这类带前导零的编码，转数值会丢失前导零。编码应保留原样字符串。

4. **去重改为“跨分块全局去重”**
   旧脚本用 `drop_duplicates()` 只在单块内去重，相同记录若落在相邻分块边界
   会各保留一次。本次用整行哈希 + 全局已见集合，实现跨块去重，结果更彻底。

---

## 7. 输出与核验

清洗后 `processing_report.json` 中 `missing_or_unknown_by_column` 记录每个字段
的缺失量、`key_field_distributions` 记录性别/支付方式等关键字段的取值分布，
可据此人工核验清洗是否达到预期。

---

## 8. 后续扩展建议

- **数据量再上一个量级**：可将“整行哈希集合”去重改为“按哈希分桶落盘再逐桶去重”，或迁移到 Spark/Dask。
- **接入下游分析平台**：本清洗结果可直接作为 `智慧医疗大数据与AI大模型分析平台` 的入库数据源。
- **增量清洗**：若数据按周期更新，可增加“按出院年份/机构号分区”的增量处理逻辑。
