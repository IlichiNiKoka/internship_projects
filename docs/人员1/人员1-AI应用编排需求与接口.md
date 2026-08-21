# 人员1：AI 应用编排需求与接口

> 前端开发与联调请直接阅读
> [人员1-前端调用指南.md](人员1-前端调用指南.md)，其中包含完整请求字段、
> 七种聊天状态、幂等重试、错误处理和报告图表渲染约定。

## 1. 范围与边界

人员1负责把人员4提供的意图识别/摘要生成能力与人员3提供的分析 API 组织成可供人员5前端直接使用的应用，不修改人员3的统计口径，也不承担人员4的模型训练与生成正确性。

实现位置：`data_process/app/application/`，对外入口：`/api/v1/assistant/*`。原 `/api/v1/ai/*` 保留为人员4模型能力和旧版兼容接口。

## 2. 功能需求

### 2.1 智能工具调用

| 项目 | 规定 |
|---|---|
| 使用场景 | 用户用自然语言发起聚合、统计、关联、费用预测、再入院风险或元数据查询 |
| 输入 | 人员4的标准意图、置信度和结构化参数 |
| 处理规则 | 使用显式 `intent -> tool` 白名单；参数先校验和转换；费用预测字段转换为算法契约；固定条件不重复分组；只对 408/429/502/503/504 等瞬态失败重试 |
| 输出 | 工具名、规范化输入、人员3原始结果、摘要适配结果、调用次数、耗时和来源信息 |
| 异常 | `unsupported` 不调用工具；缺参数返回澄清；400/404 不重试；失败结果不送入 LLM 伪装为成功 |
| 性能 | 默认最多重试 3 次；聚合默认 100 条，最大 1000 条；会话存储默认只保留结果前 200 行 |
| 验收 | 六个受支持意图均有唯一工具；LangChain 安装后注册六个 `StructuredTool`；字段转换和重试单测通过 |

意图映射：

| 意图 | 工具 | 人员3能力 |
|---|---|---|
| `aggregation_query` | `medical_aggregation` | `POST /api/v1/aggregations/run` |
| `statistics_overview` | `medical_statistics` | 算法 `statistics` |
| `association_analysis` | `medical_association` | 算法 `association` |
| `cost_prediction` | `medical_cost_prediction` | 算法 `cost_prediction` |
| `readmission_risk` | `medical_readmission_risk` | 算法 `readmission_risk` |
| `metadata_query` | `medical_metadata` | 维度/指标/算法元数据 |

### 2.2 多轮对话

| 项目 | 规定 |
|---|---|
| 使用场景 | 用户在同一 `session_id` 中继续追问“那按性别呢”“解释刚才结果”“生成刚才的报告” |
| 输入 | `message`、可选 `session_id`、可选显式 `analysis_id`、可选幂等 `request_id` |
| 处理规则 | 保存用户/助手消息和分析快照；追问继承上一轮指标/过滤条件，仅覆盖本轮明确参数；显式引用只能访问当前会话；同会话请求串行化 |
| 存储 | 开发/测试可用带 TTL/LRU 上限的内存存储；生产必须使用 Redis `SETEX` 与分布式会话锁；提供 LangChain Memory 兼容适配器 |
| 输出 | 会话 ID、消息、意图、分析 ID、结构化分析、报告、警告与上下文引用信息 |
| 异常 | 无历史却引用“刚才”时返回澄清；过期/不存在会话返回 404；工具失败保存友好消息与追踪号 |
| 性能/隐私 | 默认 TTL 24 小时；每会话最多 100 条消息、20 项分析、10 份报告；会话和报告不序列化 API key |
| 验收 | 会话恢复、隔离、追问继承、显式引用、幂等重放和并发锁均有测试 |

### 2.3 医疗洞察报告

| 项目 | 规定 |
|---|---|
| 使用场景 | 将单次或多轮分析整理为前端可渲染报告 |
| 输入 | 当前会话内一个或多个 `analysis_id`，可选报告标题 |
| 处理规则 | 从服务端已保存分析读取数据；调用人员4摘要能力；固定模板生成章节、指标卡、表格、中立图表规格、来源追踪与校验状态 |
| 输出 | `report_id`、标题、摘要、章节、图表、警告、验证结果和来源分析 ID |
| 异常 | 空来源拒绝生成；摘要幻觉校验失败时不纳入报告结论；LLM失败时使用确定性模板并标记降级 |
| 性能 | 单份报告默认最多引用 10 项分析；表格最多展示 100 行；图表 dataset 最多 200 行 |
| 验收 | 聚合、总体、关联、费用、风险五类结果都能生成稳定 JSON；所有数值均来自分析源数据 |

图表返回中立规格 `{type,dataset,encoding,series}`，由人员5转换为 ECharts 配置；服务端不返回可执行 JavaScript 或任意 formatter。

## 3. REST API

所有接口继续使用项目统一响应外壳 `{code,message,data,query_time,trace_id}`。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/v1/assistant/health` | 应用层、会话存储健康状态 |
| `GET` | `/api/v1/assistant/tools` | 工具与 LangChain 注册状态 |
| `POST` | `/api/v1/assistant/chat` | 新建/继续对话，可选同步生成报告 |
| `GET` | `/api/v1/assistant/sessions/{session_id}` | 恢复历史；`include_results=true` 可包含原分析数据 |
| `DELETE` | `/api/v1/assistant/sessions/{session_id}` | 删除会话 |
| `POST` | `/api/v1/assistant/sessions/{session_id}/reports` | 从指定或全部分析生成报告 |
| `GET` | `/api/v1/assistant/sessions/{session_id}/reports/{report_id}` | 获取报告 |

聊天请求示例：

```json
{
  "message": "2021年各医院的平均费用",
  "session_id": null,
  "analysis_id": null,
  "generate_report": true,
  "request_id": "request_12345678"
}
```

报告请求示例：

```json
{
  "analysis_ids": ["ana_0123456789abcdef0123"],
  "title": "医院费用洞察报告"
}
```

## 4. 配置

配置均使用 `ANALYTICS_` 前缀，完整示例见 `data_process/.env.example`：

- `ANALYSIS_API_MODE/BASE_URL/API_KEY/TIMEOUT`
- `ASSISTANT_API_KEY`（生产必填；支持 Bearer 或 `X-Assistant-API-Key`）
- `TOOL_MAX_ATTEMPTS/TOOL_RETRY_BASE_SECONDS/TOOL_RETRY_MAX_SECONDS`
- `CONVERSATION_BACKEND/TTL_SECONDS/MAX_*`
- `CONVERSATION_LOCK_TIMEOUT_SECONDS/BLOCKING_TIMEOUT_SECONDS/RENEW_INTERVAL_SECONDS`
- `REDIS_URL/KEY_PREFIX/SOCKET_TIMEOUT`
- `REPORT_MAX_ANALYSES`

## 5. 测试

人员1纯单元与回归测试位于 `data_process/tests/test_application.py` 和
`data_process/tests/test_application_regressions.py`，使用 Fake 分析客户端、Mock LLM 与内存/
Redis 替身，不依赖 Spark、真实 Redis 服务或网络：

```bash
python -m unittest -v \
  data_process.tests.test_application \
  data_process.tests.test_application_regressions
```
