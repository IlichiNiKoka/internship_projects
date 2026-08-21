/**
 * 后端 AI 编排层客户端（/api/v1/assistant/*）
 *
 * 对接人员1的 AI 应用编排服务：意图识别 -> 工具调用（Spark 实时取数）-> LLM 摘要。
 * 契约详见 docs/人员1/人员1-前端调用指南.md。
 *
 * 与旧实现的区别：
 *  - 不再直连 OpenAI 兼容的 /api/chat，改走后端有状态会话接口；
 *  - 后端负责工具调用与 LLM 调用，前端只负责展示文本 + 结构化结果；
 *  - session_id 由服务端生成，前端保存在 localStorage；request_id 每次发送新生成。
 */

import type { ChatMessage } from '../types/dashboard'

/** 会话 ID 存储键（同一浏览器内恢复历史会话） */
export const SESSION_STORAGE_KEY = 'assistant_session_id'

// ---------------------------------------------------------------------------
// 类型（与后端契约对齐，字段来自 人员1-前端调用指南.md）
// ---------------------------------------------------------------------------

export interface AssistantWarning {
  code: string
  message?: string
  tool?: string
  attempts?: number
  trace_id?: string
  analysis_id?: string
}

export interface AssistantReply {
  reply: string
  status: string
  sessionId: string
  intentLabel: string | null
  bullets: string[]
  table: { columns: string[]; rows: Array<Record<string, unknown>> } | null
  chart: Record<string, unknown> | null
  kpis: Array<{ label: string; value: string }> | null
  warnings: AssistantWarning[]
}

interface ApiEnvelope<T> {
  code: number
  message: string
  data: T | null
  query_time: number
  trace_id: string
}

interface ConversationMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at: string
  intent: string | null
  analysis_id: string | null
  report_id: string | null
  metadata: Record<string, unknown>
}

interface AnalysisRecord {
  id: string
  query: string
  intent: string
  tool_name: string
  tool_input: Record<string, unknown>
  result?: {
    data?: unknown
    summary_data?: unknown
    tool?: string
    request?: Record<string, unknown>
  }
  summary: Record<string, unknown>
  attempts: number
  elapsed_seconds: number
}

interface ChatResultContract {
  session_id: string
  status: string
  user_message: ConversationMessage
  assistant_message: ConversationMessage
  intent: { intent_label?: string | null; intent?: string | null; confidence?: number | null } | null
  analysis: AnalysisRecord | null
  report: unknown | null
  warnings: AssistantWarning[]
  context: Record<string, unknown>
  history_size: number
}

const ASSISTANT_BASE = '/api/v1/assistant'

export class AssistantApiError extends Error {
  public readonly status: number
  public readonly traceId: string
  public readonly detail: unknown

  constructor(status: number, traceId: string, detail: unknown, message: string) {
    super(message)
    this.name = 'AssistantApiError'
    this.status = status
    this.traceId = traceId
    this.detail = detail
  }
}

async function assistantRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(`${ASSISTANT_BASE}${path}`, { ...init, headers })
  const body = (await response.json()) as ApiEnvelope<T>
  if (!response.ok || body.code !== 200 || body.data === null) {
    throw new AssistantApiError(
      response.status,
      body.trace_id ?? response.headers.get('X-Request-ID') ?? 'unknown',
      body.data,
      body.message || '请求失败',
    )
  }
  return body.data
}

// ---------------------------------------------------------------------------
// 结构化结果渲染辅助：把 summary_data（扁平事实）转成表格 / 图表 / KPI
// ---------------------------------------------------------------------------

const LABEL_MAP: Record<string, string> = {
  total_discharges: '总住院人次',
  avg_length_of_stay: '平均住院时长(天)',
  avg_total_charges: '平均总费用(美元)',
  avg_total_costs: '平均总成本(美元)',
  emergency_rate: '急诊占比(%)',
  mae: 'MAE 平均绝对误差',
  rmse: 'RMSE 均方根误差',
  r2: 'R² 决定系数',
  predicted_total_charges: '预测总费用(美元)',
  support: '支持度',
  confidence: '置信度',
  lift: '提升度',
}

const SKIP_KPI_KEYS = new Set([
  'row_count',
  'rule_count',
  'transaction_count',
  'mode',
  'rules',
  'rows',
  'dimensions',
  'metrics',
  'filters',
  'distributions',
  'top_diseases',
])

function flatObjectArrays(summary: Record<string, unknown>): Array<Record<string, unknown>> | null {
  for (const key of ['rows', 'rules', 'distributions', 'top_diseases']) {
    const value = summary[key]
    if (Array.isArray(value) && value.length && value.every((item) => item && typeof item === 'object')) {
      return value as Array<Record<string, unknown>>
    }
  }
  return null
}

function toTable(summary: Record<string, unknown>): { columns: string[]; rows: Array<Record<string, unknown>> } | null {
  const items = flatObjectArrays(summary)
  if (!items) {
    return null
  }
  const columns = Object.keys(items[0])
  return { columns, rows: items }
}

/** 从表格列里挑出 [分组维度列, 数值指标列]，用于生成柱状图 */
function pickDimMetric(table: { columns: string[]; rows: Array<Record<string, unknown>> }): [string | null, string | null] {
  const numericKeys = table.columns.filter((col) =>
    table.rows.every((row) => typeof row[col] === 'number' || row[col] === null),
  )
  const stringKeys = table.columns.filter((col) => !numericKeys.includes(col))
  const metricKey =
    numericKeys.find((key) => /count|avg|charges|costs|support|lift|confidence|rate|pct/.test(key)) ??
    numericKeys[0] ??
    null
  const dimKey = stringKeys.find((key) => key !== metricKey) ?? stringKeys[0] ?? null
  if (!dimKey || !metricKey || dimKey === metricKey) {
    return [null, null]
  }
  return [dimKey, metricKey]
}

function toBarChart(summary: Record<string, unknown>): Record<string, unknown> | null {
  const table = toTable(summary)
  if (!table) {
    return null
  }
  const [dimCol, metricCol] = pickDimMetric(table)
  if (!dimCol || !metricCol) {
    return null
  }
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 48, right: 16, top: 24, bottom: 32 },
    xAxis: { type: 'category', data: table.rows.map((row) => String(row[dimCol] ?? '')) },
    yAxis: { type: 'value' },
    series: [
      {
        type: 'bar',
        barWidth: '46%',
        data: table.rows.map((row) => Number(row[metricCol]) || 0),
        itemStyle: { color: '#4f73d9', borderRadius: [4, 4, 0, 0] },
      },
    ],
  }
}

function toKpis(summary: Record<string, unknown>): Array<{ label: string; value: string }> | null {
  const kpis: Array<{ label: string; value: string }> = []
  for (const [key, value] of Object.entries(summary)) {
    if (SKIP_KPI_KEYS.has(key)) {
      continue
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
      kpis.push({
        label: LABEL_MAP[key] ?? key,
        value: value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }),
      })
    }
  }
  return kpis.length ? kpis : null
}

function toBullets(summary: Record<string, unknown>, limit = 5): string[] {
  const table = toTable(summary)
  if (!table) {
    return []
  }
  const [dimCol, metricCol] = pickDimMetric(table)
  if (!dimCol || !metricCol) {
    return []
  }
  return table.rows.slice(0, limit).map((row) => `${row[dimCol]}：${Number(row[metricCol]).toLocaleString()}`)
}

// ---------------------------------------------------------------------------
// 会话接口
// ---------------------------------------------------------------------------

export function loadStoredSessionId(): string | null {
  return localStorage.getItem(SESSION_STORAGE_KEY)
}

export function storeSessionId(id: string): void {
  localStorage.setItem(SESSION_STORAGE_KEY, id)
}

export function clearStoredSessionId(): void {
  localStorage.removeItem(SESSION_STORAGE_KEY)
}

/**
 * 发送一条消息到后端 AI 编排层（真实 API：意图识别 + 工具取数 + LLM 摘要）。
 * 返回归一化结果，供页面直接渲染。
 */
export async function sendAssistantChat(options: {
  message: string
  sessionId: string | null
  signal?: AbortSignal
}): Promise<AssistantReply> {
  const requestId = crypto.randomUUID()

  const result = await assistantRequest<ChatResultContract>('/chat', {
    method: 'POST',
    signal: options.signal,
    body: JSON.stringify({
      message: options.message,
      session_id: options.sessionId,
      request_id: requestId,
    }),
  })

  const summary = (result.analysis?.result?.summary_data ?? null) as Record<string, unknown> | null

  return {
    reply: result.assistant_message.content || '（后端未返回文本内容）',
    status: result.status,
    sessionId: result.session_id,
    intentLabel: result.intent?.intent_label ?? result.intent?.intent ?? null,
    bullets: summary ? toBullets(summary) : [],
    table: summary ? toTable(summary) : null,
    chart: summary ? toBarChart(summary) : null,
    kpis: summary ? toKpis(summary) : null,
    warnings: result.warnings ?? [],
  }
}

/** 兼容旧页面组件的类型引用（不再使用本地规则兜底） */
export type { ChatMessage }
