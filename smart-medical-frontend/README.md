# 智慧医疗大数据平台前端

基于 **Vue 3 + TypeScript + Vite + Pinia + ECharts** 的数据分析与 AI 对话前端，对接后端 Flask + PySpark 分析服务（`../data_process`）。前端通过 Vite 开发代理调用后端 `/api/v1` 接口，包含总览、大屏可视化、AI 智能对话三个主要页面。

## 功能概览

| 页面 | 功能 |
|---|---|
| 总览页 | 展示清洗后数据的 KPI 与核心图表（年龄结构、支付方式、重点病种、严重程度×死亡风险） |
| 大屏页 | 支持疾病/年龄/医院/年份筛选，实时调用后端聚合接口；图表缩放与下载导出 |
| 智能助手 | 自然语言提问，后端完成意图识别 → Spark 实时取数 → 大模型（DeepSeek）摘要，返回文字 + 数据表 + 图表 |

## 环境要求

- **Node.js ≥ 20.19**（Vite 8 要求）、npm ≥ 10
- 后端服务（`../data_process`）：
  - Python 3.11 / 3.12（本项目在 conda `analytics` 环境验证）
  - JDK 17（Spark 3.5.1 只兼容 Java 8/11/17）
  - DeepSeek API Key（AI 对话功能需要，可选用其他 OpenAI 兼容服务）

## 快速开始

**1. 安装前端依赖**

```bash
npm install
```

**2. 启动后端**（前端数据与分析能力依赖它，需先启动）

```bash
cd ../data_process
conda activate analytics
python run.py
```

首次启动前需在 `data_process/.env` 中配置（模板见 `.env.example`）：

```ini
# JDK 17 路径（Spark 必需，指向你的实际安装目录）
ANALYTICS_SPARK_JAVA_HOME=C:\Program Files\Eclipse Adoptium\jdk-17.x.x

# DeepSeek API Key（真实 AI 对话必需；留空则回复为模板文本）
ANALYTICS_LLM_PROVIDER=auto
ANALYTICS_LLM_API_KEY=sk-你的key
ANALYTICS_LLM_MODEL=deepseek-v4-flash
```

**3. 启动前端开发服务器**

```bash
npm run dev
```

浏览器访问 **http://localhost:5173**，登录页使用预填账号 `admin@qq.com / 123456` 进入。

## 开发代理说明

后端未启用 CORS，前端所有 `/api` 请求通过 `vite.config.ts` 中的代理转发到 `http://127.0.0.1:5000`：

```ts
server: {
  port: 5173,
  proxy: {
    '/api': { target: 'http://127.0.0.1:5000', changeOrigin: true },
  },
},
```

生产环境由 Nginx / BFF 做同样的同源转发。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_API_BASE_URL` | `/api/v1` | 后端接口前缀；开发走代理默认即可，生产可指定网关地址 |

## 页面说明

### 总览页（`/overview`）

数据来自前端静态汇总文件 `public/data/dashboard-data.json`（与后端清洗结果同源），页面打开即渲染，不依赖后端。

### 大屏页（`/big-screen`）

- 顶部筛选栏选择疾病、年龄、医院、年份后点「应用筛选」，前端把筛选条件翻译成后端聚合请求（`POST /api/v1/aggregations/run`），图表与 KPI 实时更新，状态条显示"后端实时聚合 X 条记录"。
- 后端不可用时自动回退为静态汇总展示。
- 「下载导出」可导出当前页面数据 JSON。

### 智能助手（`/assistant`）

- 提问示例：`按年龄组统计住院人次`、`平台数据总览`、`疾病与支付方式的关联`、`预测一个老年人急诊入院5天的费用`、`哪些人群再入院风险高`、`平台支持哪些分析`。
- 后端 AI 编排层（`POST /api/v1/assistant/chat`）完成意图识别、Spark 取数、DeepSeek 摘要，返回文本 + 要点 + 数据表 + 指标卡 + 图表。
- 会话由服务端维护（内存存储），`session_id` 保存在浏览器 localStorage；后端重启后旧会话失效，前端会自动清除并开启新会话。
- 「新会话」按钮可主动开始新对话。

## 生产构建

```bash
npm run build     # 类型检查 + 打包到 dist/
npm run preview   # 本地预览构建产物
```

## 常见问题

**提示"无法连接后端 AI 服务"**：确认后端已启动（`python run.py`）且 5000 端口未被旧进程占用；若提示带状态码（如 404），是旧会话失效，刷新页面或点「新会话」即可自动恢复。

**对话返回"不支持范围"**：意图识别为规则匹配，仅支持医疗数据分析类问题，闲聊（如"你是什么模型"）不在支持范围，属预期行为。

**回复是模板化文本（非自然语言）**：DeepSeek Key 未配置、无效，或后端未重启加载新配置。检查 `data_process/.env` 并重启后端。

**后端 Spark 启动失败**：确认 `ANALYTICS_SPARK_JAVA_HOME` 指向 JDK 17，且端口 5000 未被占用。

## 上传 GitHub 注意事项

仓库 `.gitignore` 已忽略以下内容，请勿强制提交：

- `node_modules`、`dist`（前端构建产物与依赖）
- `data_process/.env`（包含 API Key 等本地敏感配置；只提交 `.env.example` 模板）
- 原始与清洗后 CSV 大文件（数百 MB）、`__pycache__`、日志

推送前建议在仓库根执行 `git status` 确认没有意外的大文件或密钥文件进入暂存区。
