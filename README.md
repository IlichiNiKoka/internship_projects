# 智慧医疗大数据与 AI 大模型分析平台

> 本仓库基于真实的纽约州 SPARCS 2021 住院患者出院数据集（200 万+ 行、33 个字段），构建「数据预处理 → 大数据分析服务 → AI 智能交互 → 前端可视化」的全链路原型，覆盖项目治理、数据底座、分析服务、AI 模型与前端展示五大模块。

## 一、项目概述

| 项 | 内容 |
|---|---|
| 数据来源 | 纽约州 SPARCS（Statewide Planning and Research Cooperative System）去标识化住院出院数据 |
| 原始大小 | 约 832 MB（解压后） |
| 行数 | 2,101,588 |
| 字段数 | 33 |
| 模块 | 数据预处理与持久化 / 大数据分析服务 / AI 智能交互 / 前端可视化 |
| 技术栈 | Python 3.12、Pandas、Flask、marshmallow、PySpark 3.5.1、Spark ML Pipeline |

---

## 二、目录结构

```text
internship_projects/
├── README.md                      # 本文件：项目总览 + 代码说明
│
├── docs/                          # 需求与分工文档（全部已转为 Markdown）
│   ├── 人员分工.md                 # 团队角色、技术功能分配、协作链路
│   ├── 人员1/                      # 项目经理 / AI 应用开发（二期功能，文档待补）
│   ├── 人员2/                      # 数据预处理与持久化
│   │   ├── 人员2-数据预处理与持久化模块需求.md
│   │   └── 人员2-软件需求规约-功能设计.md
│   ├── 人员3/                      # 分析服务与系统后端
│   │   └── 人员3-软件需求规约-功能设计.md
│   ├── 人员4/                      # 模型与智能分析
│   │   ├── 人员4需求文档.md
│   │   ├── 人员4用例图文字图例.md
│   │   └── 人员4流程图文字图例.md
│   └── 人员5/                      # 前端可视化与独立测试
│       └── 人员5-前端具体需求.md
│
└── data_process/                  # 代码实现（详见下文「代码实现说明」）
    ├── README.md                  # 子模块说明
    ├── requirements.txt
    ├── .env.example
    ├── pytest.ini
    ├── run.py                     # 分析服务开发入口
    ├── wsgi.py                    # 分析服务生产入口
    ├── run_pipeline.py             # 数据清洗入口
    │
    ├── # ---- 数据清洗流水线 ----
    ├── cleaning_config.py
    ├── cleaners.py
    ├── medical_data_pipeline.py
    ├── data_dictionary.md
    ├── processing_reason.txt
    │
    ├── config/                    # 分析服务配置（settings.py + registry.py）
    ├── app/                       # 应用主包（API + 服务层 + 算法 + 数据层 + 核心层）
    ├── scripts/                   # 工具脚本（冒烟测试等）
    ├── tests/                     # pytest 测试套件
    ├── raw/                       # 原始数据（已解压）
    └── processed/                 # 输出：清洗结果 + 分析服务数据源
```

---

## 三、人员分工

> 完整内容见 [docs/人员分工.md](docs/人员分工.md)，以下为角色总览与协作链路。

### 团队角色总览

| 人员 | 主要角色 | 兼任角色 | 技术功能数 | 核心定位 |
|---|---|---|---|---|
| 人员1 | 项目经理 | 后端工程师、配置管理工程师 | 3 项 | 项目治理 + AI 应用开发 |
| 人员2 | 数据处理工程师 | 数据采集工程师、数据库设计工程师 | 7 项 | 数据底座负责人 |
| 人员3 | 系统设计工程师 | 后端工程师、数据分析工程师 | 5 项 | 分析服务与应用后端负责人 |
| 人员4 | 模型训练工程师 | 数据分析工程师、后端工程师 | 5 项 | AI 模型与智能分析负责人 |
| 人员5 | 前端工程师 | 界面设计工程师、测试工程师、模型测试工程师 | 3 项 + 独立测试 | 产品交互与独立质量负责人 |

### 协作链路

```text
人员2 数据底座 → 人员3 分析服务与后端 → 人员4 AI 模型 → 人员1 AI 应用编排 → 人员5 页面与独立测试
```

- 人员1 贯穿全流程，负责需求协调、计划、变更、版本和最终验收。
- 每项功能都有唯一主责人，需求由主责人自行编写，人员1 仅负责汇总和冲突消除。

### 需求文档导航

| 章节 | 编写人 | 文档路径 |
|---|---|---|
| 项目范围、里程碑、版本与总体流程 | 人员1 | [docs/人员分工.md](docs/人员分工.md) |
| 前端三项功能（大屏 / 聊天 / 进阶） | 人员5（由人员1 起草） | [docs/人员5/人员5-前端具体需求.md](docs/人员5/人员5-前端具体需求.md) |
| 数据接入、清洗、入库、质量、备份 | 人员2 | [docs/人员2/人员2-数据预处理与持久化模块需求.md](docs/人员2/人员2-数据预处理与持久化模块需求.md)<br>[docs/人员2/人员2-软件需求规约-功能设计.md](docs/人员2/人员2-软件需求规约-功能设计.md) |
| 分析指标、算法、API、缓存和异常 | 人员3 | [docs/人员3/人员3-软件需求规约-功能设计.md](docs/人员3/人员3-软件需求规约-功能设计.md) |
| 意图识别、预测模型、LLM 部署 | 人员4 | [docs/人员4/人员4需求文档.md](docs/人员4/人员4需求文档.md)<br>[docs/人员4/人员4用例图文字图例.md](docs/人员4/人员4用例图文字图例.md)<br>[docs/人员4/人员4流程图文字图例.md](docs/人员4/人员4流程图文字图例.md) |
| AI 工具调用、多轮对话、报告生成 | 人员1 | 二期功能，文档待补 |

---

## 四、代码实现说明

代码集中位于 [data_process/](data_process/)，由两个相互衔接的子系统组成：

1. **数据清洗流水线**（根目录脚本，人员2）：对 SPARCS 2021 数据进行分块读取、清洗、标准化、跨块去重，产出统一字段类型、统一取值域、去重后的高质量 CSV，作为下游分析服务的唯一数据源。
2. **大数据分析服务**（`app/` 子目录，人员3）：基于 Flask + PySpark 的 REST 服务，对清洗后数据提供多维度聚合、统计指标、关联分析、住院费用预测、再入院风险评估等接口，供上层 AI 智能层（人员1 / 人员4）调用。

### 4.1 数据清洗流水线（人员2）

| 文件 | 职责 |
|---|---|
| [data_process/cleaning_config.py](data_process/cleaning_config.py) | 集中配置：路径、`CHUNK_SIZE=200_000`、字段分类（TEXT / CODE / PAYMENT / INT / MONEY）、性别与急诊标志映射表 |
| [data_process/cleaners.py](data_process/cleaners.py) | 清洗函数库（纯函数）：`normalize_column_name`、`clean_text`、`standardize_gender`、`standardize_yn`、`to_nullable_int`、`to_money`、`clean_chunk` |
| [data_process/medical_data_pipeline.py](data_process/medical_data_pipeline.py) | 主流水线：`find_source_csv` 递归定位、`_deduplicate` 整行哈希跨块全局去重、流式追加写 CSV、`build_report` 生成处理报告 JSON |
| [data_process/run_pipeline.py](data_process/run_pipeline.py) | 入口脚本：`python run_pipeline.py` |
| [data_process/data_dictionary.md](data_process/data_dictionary.md) | 33 个字段的数据字典（类型 / 取值 / 清洗规则 / 处理原因） |
| [data_process/processing_reason.txt](data_process/processing_reason.txt) | 清洗需求说明 |

**清洗规则总览（做了什么 + 为什么）**

| # | 步骤 | 怎么做 | 为什么 |
|---|---|---|---|
| 1 | 列名统一 | `"Total Charges"` → `total_charges`，`"Zip Code - 3 digits"` → `zip_code_3_digits` | 原始列名含空格 / 连字符 / 括号，统一成小写下划线后全链路字段名一致 |
| 2 | 文本去空白 | 折叠内部空白（含 NBSP）并去首尾空白 | `"Male "` 与 `"Male"` 会被当成两个值，导致统计分裂 |
| 3 | 缺失统一 | 文本/分类字段缺失 → `Unknown`；数值字段缺失 → `NaN` | 缺失以空串 / `nan` / `None` 多种形态出现，统一哨兵便于过滤与统计 |
| 4 | 编码保前导零 | 机构号、邮编、DRG / MDC / CCSR 码保留字符串原样 | 这些是编码 / 标识而非测量值，前导零有意义，转数值会丢失 |
| 5 | 整数转数值 | 住院天数、出院年份、严重程度代码、出生体重转整数 | 转成数值后才能做大小比较、求和、均值 |
| 6 | 金额转数值 | `"320,922.43"` → `320922.43`（浮点） | 金额带千分位逗号且是字符串，不转数值无法求和 / 建模 |
| 7 | 性别标准化 | `M / F / U` → `Male / Female / Unknown` | 统一取值域，避免缩写混用导致统计分裂 |
| 8 | 急诊标志标准化 | 只保留 `Y / N`，其余 → `Unknown` | 保证"是否急诊"严格二值，脏值不混入布尔判断 |
| 9 | 跨分块去重 | 用整行内容哈希做流式全局去重 | 去掉完全重复的住院记录，避免重复统计；跨块去重比单块内更彻底 |

**关键设计决策（相对旧脚本的修正）**

1. `APR Risk of Mortality` 是文本字段（`Minor / Moderate / Major / Extreme`），不是数字——若按数值转换会整列变 NaN，丢失关键信息。
2. `Zip Code - 3 digits` 含 `OOS`（州外）这一合法取值——按数值转换会把约 5.9 万条 `OOS` 全部变 NaN，故保留为字符串。
3. `APR DRG Code` / `APR MDC Code` 保留字符串，前导零不丢。
4. 去重改为「跨分块全局去重」——旧脚本用 `drop_duplicates()` 只在单块内去重，相同记录若落在相邻分块边界会各保留一次；本次用整行哈希 + 全局已见集合，结果更彻底。

**输出与核验**

清洗后 `processed/processing_report.json` 中：

- `rows.raw_rows / clean_rows / duplicate_rows_removed` —— 原始 / 清洗 / 去重行数
- `missing_or_unknown_by_column` —— 每个字段的缺失量
- `key_field_distributions` —— 性别 / 支付方式等关键字段的取值分布
- `processing_rules` —— 清洗规则结构化摘要

### 4.2 大数据分析服务（人员3）

#### 4.2.1 应用入口与配置

| 文件 | 职责 |
|---|---|
| [data_process/app/__init__.py](data_process/app/__init__.py) | 应用工厂 `create_app(settings, data_provider)`：日志 → Flask → 扩展容器 → 数据源 → 算法注册 → 蓝本 → 中间件，支持依赖注入 |
| [data_process/app/extensions.py](data_process/app/extensions.py) | 进程级单例容器 `_Extensions`：`settings / data_provider / cache` |
| [data_process/run.py](data_process/run.py) | 开发入口：`python run.py`，默认监听 `127.0.0.1:5000` |
| [data_process/wsgi.py](data_process/wsgi.py) | 生产入口：`gunicorn -w 2 -b 0.0.0.0:5000 wsgi:app --timeout 120` |
| [data_process/config/settings.py](data_process/config/settings.py) | `Settings` dataclass：env / host / port / data_csv_path / Spark 参数 / 缓存参数 / 聚合限制 / ML 参数 / 日志，支持 `.env` + `ANALYTICS_` 环境变量覆盖 |
| [data_process/.env.example](data_process/.env.example) | 环境变量示例 |
| [data_process/app/utils/spark.py](data_process/app/utils/spark.py) | `build_spark_session`：跨平台 JAVA_HOME 探测（Linux/macOS/Windows）、`PYSPARK_PYTHON` 锁定当前解释器 |
| [data_process/app/utils/logging_conf.py](data_process/app/utils/logging_conf.py) | 控制台 + 按大小轮转文件日志，`TraceIdFilter` 注入 trace_id |

#### 4.2.2 数据层

| 文件 | 职责 |
|---|---|
| [data_process/app/data/data_provider.py](data_process/app/data/data_provider.py) | `DataProvider` 抽象 + `SparkDataProvider`（生产）+ `MemoryDataProvider`（测试替身）。`SPARCS_SCHEMA` 显式声明 33 字段类型；`SparkDataProvider._load` 内 `df.cache()` 全表物化，后续聚合全部命中内存缓存 |

#### 4.2.3 核心层（响应标准化 / 错误码 / 异常 / 缓存 / 中间件）

| 文件 | 职责 |
|---|---|
| [data_process/app/core/response.py](data_process/app/core/response.py) | 统一 JSON 响应封装：`build / success / error`，结构 = `{code, message, data, query_time, trace_id}` |
| [data_process/app/core/error_codes.py](data_process/app/core/error_codes.py) | `ErrorCode` 枚举与默认消息：2xx 成功 / 4xx 客户端错误 / 5xx 服务端错误，HTTP 状态码与响应体 `code` 一致 |
| [data_process/app/core/exceptions.py](data_process/app/core/exceptions.py) | 业务异常体系：`BizException` 基类 + `ParamValidationError` / `InvalidDimensionError` / `InvalidMetricError` / `InvalidFilterError` / `ResourceNotFoundError` / `AlgorithmNotFoundError` / `ComputationError` / `ServiceUnavailableError` / `ComputationTimeoutError` |
| [data_process/app/core/cache.py](data_process/app/core/cache.py) | `CacheBackend` 抽象 + `InMemoryTTLCache`（线程安全 TTL + LRU + 空结果哨兵防穿透）+ `NullCache`，二期可无缝切换 Redis |
| [data_process/app/core/middleware.py](data_process/app/core/middleware.py) | `before_request`（trace_id 生成 / 透传 / 计时 / 访问日志）+ `after_request`（响应头回写 + 兜底包装非标准响应）+ 四层 `errorhandler`（`BizException` / `ValidationError` / `HTTPException` / 未预期异常） |

#### 4.2.4 维度 / 指标注册表

| 文件 | 职责 |
|---|---|
| [data_process/config/registry.py](data_process/config/registry.py) | 维度白名单（22 项：年份 / 年龄组 / 性别 / 种族 / 族裔 / 邮编 / 服务区域 / 县 / 医院 / 疾病编码 / 疾病描述 / 操作描述 / DRG / MDC / 严重程度 / 死亡风险 / 内外科 / 入院类型 / 出院去向 / 急诊标志 / 三档支付方式）+ 指标白名单（9 项：`discharge_count` / `avg_length_of_stay` / `max_length_of_stay` / `avg_total_charges` / `sum_total_charges` / `avg_total_costs` / `sum_total_costs` / `avg_birth_weight` / `avg_severity_of_illness`）+ 过滤操作符白名单 + `SORTABLE_FIELDS` + `NUMERIC_FILTER_COLUMNS` |

#### 4.2.5 服务层

| 文件 | 职责 |
|---|---|
| [data_process/app/services/aggregation_service.py](data_process/app/services/aggregation_service.py) | 多维度聚合分析核心服务：参数白名单校验 → 缓存查询 → Spark `groupBy + agg` → 结果归一化（数值精度、NaN→null、字段命名）→ 写缓存；不依赖 Flask，可被 REST API 与算法组件复用 |
| [data_process/app/services/algorithm_service.py](data_process/app/services/algorithm_service.py) | 算法调度服务：查注册表 → `ParamSpec` 校验 → 执行 → `AlgorithmResult.to_dict()` 归一化 |

#### 4.2.6 算法组件库（人员3 框架 + 人员4 复杂分析）

| 文件 | 算法 | 负责人 | 关键能力 |
|---|---|---|---|
| [data_process/app/algorithms/base.py](data_process/app/algorithms/base.py) | 统一框架 | 人员3 | `Algorithm` 基类 + `ParamSpec` 声明式参数规格 + `AlgorithmContext` / `AlgorithmResult` 契约 + `@register_algorithm` 装饰器 + `register_builtin_algorithms` 幂等注册 |
| [data_process/app/algorithms/group_aggregation.py](data_process/app/algorithms/group_aggregation.py) | `group_aggregation` | 人员3 | 把 3.3.1 聚合逻辑以统一算法接口封装，API 与 AI 智能交互共用同一实现 |
| [data_process/app/algorithms/statistics.py](data_process/app/algorithms/statistics.py) | `statistics` | 人员3 | 总体指标（人次 / 平均住院 / 平均费用 / 平均成本 / 急诊率 / 平均严重度）+ 6 维分布 + Top 疾病 |
| [data_process/app/algorithms/association.py](data_process/app/algorithms/association.py) | `association` | 人员4 | 疾病-操作关联规则挖掘（Apriori-lite）：support / confidence / lift Top-N 规则；前件 / 后件字段走白名单 |
| [data_process/app/algorithms/cost_prediction.py](data_process/app/algorithms/cost_prediction.py) | `cost_prediction` | 人员4 | 住院费用预测（Spark ML Pipeline 线性回归）：train 模式输出 RMSE / MAE / R² + 特征系数，predict 模式输出单点预测；进程内模型缓存，相同参数不重复训练 |
| [data_process/app/algorithms/readmission_risk.py](data_process/app/algorithms/readmission_risk.py) | `readmission_risk` | 人员4 | 患者再入院风险评分：profile 模式输出人群画像（年龄组 × 入院类型平均风险分 + 等级分布 + 高风险年龄组），score 模式输出单条评估与维度贡献；脱敏数据无患者 ID，一期为规则评分代理实现，二期可平滑升级为真实预测模型 |

#### 4.2.7 请求 / 响应 Schema

| 文件 | 职责 |
|---|---|
| [data_process/app/schemas/aggregation.py](data_process/app/schemas/aggregation.py) | `AggregationRequestSchema` + `FilterSchema` + `SortItemSchema`：marshmallow 结构与类型校验 |
| [data_process/app/schemas/algorithm.py](data_process/app/schemas/algorithm.py) | `AlgorithmRunSchema`：params 为自由字典，具体校验由 `ParamSpec` 完成 |

#### 4.2.8 REST API

| 文件 | 路由 | 说明 |
|---|---|---|
| [data_process/app/api/v1/health.py](data_process/app/api/v1/health.py) | `GET /api/v1/health` | 服务健康检查（含数据源行数 / 加载耗时 / 缓存命中率） |
| [data_process/app/api/v1/meta.py](data_process/app/api/v1/meta.py) | `GET /api/v1/meta/dimensions`<br>`GET /api/v1/meta/metrics`<br>`GET /api/v1/meta/algorithms`<br>`GET /api/v1/meta/cache` | 维度 / 指标 / 算法 / 缓存元数据，供 AI 智能交互模块动态发现可用分析能力 |
| [data_process/app/api/v1/aggregation.py](data_process/app/api/v1/aggregation.py) | `POST /api/v1/aggregations/run` | 多维度聚合分析（dimensions / metrics / filters / sort / limit） |
| [data_process/app/api/v1/algorithms.py](data_process/app/api/v1/algorithms.py) | `POST /api/v1/algorithms/<name>/run`<br>`GET /api/v1/algorithms/<name>` | 统一算法执行接口 + 算法元信息查询 |

### 4.3 测试

| 文件 | 职责 |
|---|---|
| [data_process/tests/conftest.py](data_process/tests/conftest.py) | pytest 全局 fixture：`spark` / `sample_df`（600 行内存构造数据，行号 i 与取值存在确定性函数关系）/ `app` / `client`，不依赖真实 210 万行 CSV |
| [data_process/tests/test_aggregation_service.py](data_process/tests/test_aggregation_service.py) | 聚合服务层用例 |
| [data_process/tests/test_algorithms.py](data_process/tests/test_algorithms.py) | 算法组件用例 |
| [data_process/tests/test_api.py](data_process/tests/test_api.py) | REST API 用例 |
| [data_process/tests/test_error_codes.py](data_process/tests/test_error_codes.py) | 错误码用例 |
| [data_process/tests/test_response.py](data_process/tests/test_response.py) | 响应标准化用例 |
| [data_process/tests/test_schemas.py](data_process/tests/test_schemas.py) | Schema 校验用例 |
| [data_process/pytest.ini](data_process/pytest.ini) | 测试配置：`pythonpath = .`、`testpaths = tests`、markers `spark` / `slow` |
| [data_process/scripts/smoke_test.py](data_process/scripts/smoke_test.py) | 真实数据冒烟测试：健康检查 → 聚合 → 过滤聚合 → 统计算法 → 关联分析 → 再入院画像，全部走服务层代码 |

### 4.4 环境与运行

**依赖**

- Python ≥ 3.12
- 依赖：pandas、Flask、marshmallow、python-dotenv、pyspark、pytest（详见 [data_process/requirements.txt](data_process/requirements.txt)）
- Java 8 / 11 / 17（Spark 3.5.1 要求，推荐 17；`app/utils/spark.py` 跨平台自动探测）

**安装与运行**

```bash
# 1) 创建虚拟环境并安装依赖
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r data_process/requirements.txt

# 2) 数据清洗流水线（产出 processed/*_clean.csv 与 processing_report.json）
cd data_process && python run_pipeline.py

# 3) 大数据分析服务（开发环境）
python run.py

# 3') 生产环境（Linux + gunicorn）
gunicorn -w 2 -b 0.0.0.0:5000 wsgi:app --timeout 120

# 4) 测试
pytest                       # 全部用例
pytest -m "not spark"        # 跳过依赖 Spark 的用例

# 5) 真实数据冒烟测试（首次加载约 1~2 分钟）
python scripts/smoke_test.py
```

**主要接口**（默认监听 `http://127.0.0.1:5000`）

- `GET  /api/v1/health` — 服务健康检查
- `GET  /api/v1/meta/dimensions` — 22 个可用维度
- `GET  /api/v1/meta/metrics` — 9 个聚合指标
- `GET  /api/v1/meta/algorithms` — 已注册算法
- `POST /api/v1/aggregations/run` — 多维度聚合
- `POST /api/v1/algorithms/<name>/run` — 算法执行（statistics / association / cost_prediction / readmission_risk / group_aggregation）
- `GET  /api/v1/algorithms/<name>` — 算法元信息（参数规格 / 说明）

### 4.5 二期规划（仓库尚未实现）

| 模块 | 规划内容 | 主责人 |
|---|---|---|
| 数据底座 | 数据质量评估（4 维度评分 + 可视化报告）、数据备份与恢复、大数据增量更新（MySQL / HDFS 入库） | 人员2 |
| 分析服务 | 接入 Redis 替换进程内缓存；接入 MySQL / HDFS 数据底座；扩展 `DataProvider` 子类 | 人员3 |
| AI 模型 | 基础自然语言意图识别（准确率 ≥ 90%）、意图识别优化（模糊 / 多维 / 医疗术语联想）、LLM 本地化部署（Qwen / BaiChuan + FastAPI） | 人员4 |
| AI 应用 | 智能工具调用（LangChain Tool 注册 + 意图-API 映射 + 重试）、多轮对话（会话 ID + Redis/LangChain Memory）、医疗洞察报告生成 | 人员1 |
| 前端 | 大屏可视化（ECharts KPI / 联动 / 下载）、Web 聊天界面（自然语言 + 图表展示）、可视化进阶（3D / 地图 / 时序） | 人员5 |
| 独立测试 | 覆盖全部 23 项功能的集成测试、端到端测试、模型测试集与发布建议 | 人员5 |

---

## 五、统一响应示例

**成功响应**（`code` 与 HTTP 状态码一致）

```json
{
  "code": 200,
  "message": "OK",
  "data": {
    "dimensions": [{"key": "age_group", "column": "age_group", "label": "年龄组"}],
    "metrics": [{"key": "discharge_count", "label": "住院人次", "unit": "人次"}],
    "rows": [{"age_group": "70 or Older", "discharge_count": 123456, "avg_total_charges": 45678.90}],
    "row_count": 5,
    "cached": false,
    "compute_seconds": 1.234
  },
  "query_time": 1.512,
  "trace_id": "a1b2c3d4e5f67890"
}
```

**错误响应**（参数非法示例）

```json
{
  "code": 400,
  "message": "包含不支持的聚合维度",
  "data": {"detail": {"invalid_dimensions": ["nonexistent"], "available": ["age_group", "gender", "..."]}},
  "query_time": 0.012,
  "trace_id": "b2c3d4e5f67890a1"
}
```

---

## 六、与需求文档的对应关系

| 需求编号 | 功能 | 文档 | 代码位置 |
|---|---|---|---|
| 3.2.1 | 大数据批量读取 | [人员2-需求](docs/人员2/人员2-数据预处理与持久化模块需求.md#uc1-读取医疗大数据) | [medical_data_pipeline.py](data_process/medical_data_pipeline.py) |
| 3.2.2 | 大数据异常处理 | [人员2-需求](docs/人员2/人员2-数据预处理与持久化模块需求.md#uc2-清洗缺失值--异常值) | [cleaners.py](data_process/cleaners.py) |
| 3.2.3 | 数据类型标准化 | [人员2-需求](docs/人员2/人员2-数据预处理与持久化模块需求.md#uc3-标准化数据类型) | [cleaners.py](data_process/cleaners.py) |
| 3.2.4 | 结构化大数据入库 | [人员2-需求](docs/人员2/人员2-数据预处理与持久化模块需求.md#uc4-输出结构化数据一期清洗后-csv) | [medical_data_pipeline.py](data_process/medical_data_pipeline.py) + [data_provider.py](data_process/app/data/data_provider.py) |
| 3.3.1 | 多维度聚合分析 | [人员3-规约](docs/人员3/人员3-软件需求规约-功能设计.md#331-多维度聚合分析) | [aggregation.py](data_process/app/api/v1/aggregation.py) + [aggregation_service.py](data_process/app/services/aggregation_service.py) + [registry.py](data_process/config/registry.py) |
| 3.3.2 | 大数据算法封装 | [人员3-规约](docs/人员3/人员3-软件需求规约-功能设计.md#332-大数据算法封装) | [algorithms/base.py](data_process/app/algorithms/base.py) + [algorithm_service.py](data_process/app/services/algorithm_service.py) + [algorithms.py](data_process/app/api/v1/algorithms.py) |
| 3.3.3 | API 响应标准化 | [人员3-规约](docs/人员3/人员3-软件需求规约-功能设计.md#333-api响应标准化) | [response.py](data_process/app/core/response.py) + [error_codes.py](data_process/app/core/error_codes.py) + [middleware.py](data_process/app/core/middleware.py) |
| 3.3.4 | API 性能优化 | [人员3-规约](docs/人员3/人员3-软件需求规约-功能设计.md#334-api性能优化) | [cache.py](data_process/app/core/cache.py) + [data_provider.py](data_process/app/data/data_provider.py) |
| 3.3.5 | API 异常处理机制 | [人员3-规约](docs/人员3/人员3-软件需求规约-功能设计.md#335-api异常处理机制) | [exceptions.py](data_process/app/core/exceptions.py) + [middleware.py](data_process/app/core/middleware.py) |
| 3.4.x | 复杂医疗大数据分析（疾病关联 / 费用预测 / 再入院风险） | [人员4-需求](docs/人员4/人员4需求文档.md) | [association.py](data_process/app/algorithms/association.py) + [cost_prediction.py](data_process/app/algorithms/cost_prediction.py) + [readmission_risk.py](data_process/app/algorithms/readmission_risk.py) |
| 3.5.x | 前端三项功能 | [人员5-前端需求](docs/人员5/人员5-前端具体需求.md) | 二期实现（待人员5 开发） |

---

*本 README 由 `docs/` 下全部需求文档（已转为 Markdown）与 `data_process/` 仓库代码现状整合而成，作为项目入口与导航。详细需求规约请进入对应人员子目录阅读，详细代码说明请进入 [data_process/README.md](data_process/README.md) 与各源文件 docstring。*
