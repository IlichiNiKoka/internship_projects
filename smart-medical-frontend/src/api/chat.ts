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
import { ErrorCode, defaultMessage, isAuthError, isRetryableError } from './errorCodes'

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
  code: ErrorCode
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

/**
 * 把 summary_data（扁平事实）归一化为前端可渲染的表格/图表/KPI。
 * 供实时回复与会话恢复两条路径复用，保证返回后结构化结果一致联动。
 */
function renderSummaryData(summary: Record<string, unknown> | null): Pick<
  AssistantReply,
  'bullets' | 'table' | 'chart' | 'kpis'
> {
  return {
    bullets: summary ? toBullets(summary) : [],
    table: summary ? toTable(summary) : null,
    chart: summary ? toBarChart(summary) : null,
    kpis: summary ? toKpis(summary) : null,
  }
}

export class AssistantApiError extends Error {
  public readonly code: ErrorCode
  public readonly status: number
  public readonly traceId: string
  public readonly detail: unknown
  public readonly retryAfter?: number

  constructor(code: ErrorCode, status: number, traceId: string, detail: unknown, message: string, retryAfter?: number) {
    super(message)
    this.name = 'AssistantApiError'
    this.code = code
    this.status = status
    this.traceId = traceId
    this.detail = detail
    this.retryAfter = retryAfter
  }

  /** 是否为认证错误 (401/403) */
  isAuthError(): boolean {
    return isAuthError(this.code)
  }

  /** 是否为可重试错误 (429/503/504) */
  isRetryable(): boolean {
    return isRetryableError(this.code)
  }

  /** 获取用户友好的错误消息 */
  getUserMessage(): string {
    if (this.message) return this.message
    return defaultMessage(this.code)
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

  // 后端标准响应：code/message/data/query_time/trace_id
  // 成功：code === 200 且 data !== null
  // 失败：code !== 200 或 data === null
  const code = body.code ?? (response.ok ? ErrorCode.OK : ErrorCode.INTERNAL_ERROR)

  if (!response.ok || code !== ErrorCode.OK || body.data === null) {
    // 尝试从 Retry-After 头获取重试等待时间（429 错误）
    const retryAfterHeader = response.headers.get('Retry-After')
    const retryAfter = retryAfterHeader ? parseInt(retryAfterHeader, 10) : undefined

    throw new AssistantApiError(
      code,
      response.status,
      body.trace_id ?? response.headers.get('X-Request-ID') ?? 'unknown',
      body.data,
      body.message ?? defaultMessage(code),
      retryAfter,
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

/** 幂等 request_id：后端要求 ^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$ */
export function newRequestId(): string {
  return `req-${crypto.randomUUID().replace(/-/g, '').slice(0, 24)}`
}

// ---------------------------------------------------------------------------
// 会话恢复 / 删除（多轮对话：会话恢复与删除接口）
// ---------------------------------------------------------------------------

interface SessionSnapshotContract {
  session_id: string
  created_at: string
  updated_at: string
  messages: ConversationMessage[]
  analyses: AnalysisRecord[]
  reports: Array<{ report_id: string; title?: string; generated_at?: string }>
}

export interface RestoredMessage {
  role: 'user' | 'assistant'
  content: string
  createdAt: number
  intentLabel: string | null
  status: 'ok' | 'error'
  /** 与该消息关联的结构化结果（仅助手消息可能有），返回后可联动展示 */
  bullets: string[]
  table: { columns: string[]; rows: Array<Record<string, unknown>> } | null
  chart: Record<string, unknown> | null
  kpis: Array<{ label: string; value: string }> | null
}

export interface RestoredSession {
  messages: RestoredMessage[]
  /** 会话内已生成的报告 ID（按生成顺序，最后一个为最新） */
  reportIds: string[]
}

/** 会话恢复：拉取服务端会话快照，还原消息列表及其结构化结果。 */
export async function fetchAssistantSession(sessionId: string): Promise<RestoredSession> {
  const snapshot = await assistantRequest<SessionSnapshotContract>(
    `/sessions/${encodeURIComponent(sessionId)}?include_results=true`,
  )
  // analysis_id -> 分析记录索引，用于把助手消息与其结构化结果重新关联
  const analysisById = new Map<string, AnalysisRecord>()
  for (const item of snapshot.analyses ?? []) {
    if (item && typeof item.id === 'string') {
      analysisById.set(item.id, item)
    }
  }
  const messages = (snapshot.messages ?? [])
    .filter((item) => item.role === 'user' || item.role === 'assistant')
    .map((item) => {
      const analysis = item.analysis_id ? analysisById.get(item.analysis_id) : undefined
      const analysisResult = analysis?.result as { summary_data?: unknown; data?: unknown } | undefined
      const summaryData = (
        analysisResult?.summary_data
          ?? (analysisResult?.data as Record<string, unknown> | undefined)
          ?? null
      ) as Record<string, unknown> | null
      return {
        role: item.role,
        content: item.content,
        createdAt: Date.parse(item.created_at) || Date.now(),
        intentLabel: item.intent ?? null,
        status: 'ok' as const,
        ...(item.role === 'assistant' ? renderSummaryData(summaryData) : {
          bullets: [], table: null, chart: null, kpis: null,
        }),
      }
    })
  return {
    messages,
    reportIds: (snapshot.reports ?? [])
      .map((item) => item.report_id)
      .filter((id): id is string => Boolean(id)),
  }
}

/** 会话删除：删除服务端会话及其全部历史与分析快照。 */
export async function deleteAssistantSession(sessionId: string): Promise<void> {
  await assistantRequest(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
}

// ---------------------------------------------------------------------------
// 医疗洞察报告（结构化 JSON 报告的拉取与渲染契约）
// ---------------------------------------------------------------------------

export interface ReportKeyMetric {
  key: string
  label: string
  value: number | string
  unit: string
}

export interface ReportSection {
  section_id: string
  analysis_id: string
  intent: string
  title: string
  query: string
  narrative: string
  key_metrics: ReportKeyMetric[]
  table: { columns: string[]; rows: Array<Record<string, unknown>> } | null
  chart_ids: string[]
  summary_validation?: { trusted?: boolean }
}

/** 后端中立的图表规格（不绑定具体图表库，由前端转换为 ECharts option） */
export interface NeutralChartSpec {
  chart_id: string
  analysis_id: string
  type: 'bar' | 'pie' | 'scatter' | 'gauge' | 'kpi'
  title: string
  dataset: Array<Record<string, unknown>>
  encoding: Record<string, unknown>
  series: Array<{ field: string; name: string }>
}

export interface MedicalInsightReport {
  schema_version: string
  report_id: string
  session_id: string
  title: string
  generated_at: string
  source_analysis_ids: string[]
  executive_summary: string
  sections: ReportSection[]
  charts: NeutralChartSpec[]
  warnings: Array<{ code: string; message?: string; analysis_id?: string }>
  validation: { all_summaries_trusted: boolean; warning_count: number; source_count: number }
  provenance: Array<{
    analysis_id: string
    tool: string
    tool_input: Record<string, unknown>
    attempts: number
    elapsed_seconds: number
    called_at?: string
  }>
}

/** 从指定或全部分析生成医疗洞察报告（POST /sessions/{id}/reports）。 */
export async function generateAssistantReport(
  sessionId: string,
  options: { analysisIds?: string[]; title?: string } = {},
): Promise<MedicalInsightReport> {
  return assistantRequest<MedicalInsightReport>(
    `/sessions/${encodeURIComponent(sessionId)}/reports`,
    {
      method: 'POST',
      body: JSON.stringify({
        ...(options.analysisIds?.length ? { analysis_ids: options.analysisIds } : {}),
        ...(options.title ? { title: options.title } : {}),
      }),
    },
  )
}

/** 读取已保存的洞察报告（GET /sessions/{id}/reports/{report_id}）。 */
export async function getAssistantReport(
  sessionId: string,
  reportId: string,
): Promise<MedicalInsightReport> {
  return assistantRequest<MedicalInsightReport>(
    `/sessions/${encodeURIComponent(sessionId)}/reports/${encodeURIComponent(reportId)}`,
  )
}

// ---------------------------------------------------------------------------
// 报告导出：JSON / Markdown（前端确定性转换，不改后端契约）
// ---------------------------------------------------------------------------

function downloadFile(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function safeFilename(title: string): string {
  return (title || 'medical-insight-report').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 60)
}

/** 导出洞察报告为 JSON 文件（与后端结构化契约完全一致）。 */
export function exportReportJson(report: MedicalInsightReport): void {
  downloadFile(
    `${safeFilename(report.title)}_${report.report_id}.json`,
    JSON.stringify(report, null, 2),
    'application/json',
  )
}

/** 导出洞察报告为 Markdown 文件（便于直接阅读/贴入文档）。 */
export function exportReportMarkdown(report: MedicalInsightReport): void {
  const lines: string[] = [
    `# ${report.title}`,
    '',
    `- 报告 ID：\`${report.report_id}\``,
    `- 会话 ID：\`${report.session_id}\``,
    `- 生成时间：${report.generated_at}`,
    `- 数据来源分析：${report.source_analysis_ids.join(', ')}`,
    '',
    '## 执行摘要',
    '',
    report.executive_summary,
    '',
  ]
  for (const section of report.sections ?? []) {
    lines.push(`## ${section.title}`, '', `> 提问：${section.query}`, '')
    lines.push(section.narrative, '')
    if (section.key_metrics?.length) {
      lines.push(
        '| 指标 | 数值 | 单位 |',
        '| --- | --- | --- |',
        ...section.key_metrics.map((metric) =>
          `| ${metric.label} | ${metric.value} | ${metric.unit || '-'} |`,
        ),
        '',
      )
    }
    const table = section.table
    if (table?.rows?.length) {
      lines.push(
        `| ${table.columns.join(' | ')} |`,
        `| ${table.columns.map(() => '---').join(' | ')} |`,
        ...table.rows.map((row) =>
          `| ${table.columns.map((col) => String(row[col] ?? '')).join(' | ')} |`,
        ),
        '',
      )
    }
  }
  if (report.warnings?.length) {
    lines.push('## 警告', '')
    for (const warning of report.warnings) {
      lines.push(`- ⚠ ${warning.message ?? warning.code}`)
    }
    lines.push('')
  }
  if (report.provenance?.length) {
    lines.push(
      '## 数据来源与参数', '',
      '| 分析 ID | 工具 | 耗时(s) | 尝试次数 |',
      '| --- | --- | --- | --- |',
      ...report.provenance.map((item) =>
        `| ${item.analysis_id} | ${item.tool} | ${item.elapsed_seconds.toFixed(2)} | ${item.attempts} |`,
      ),
    )
  }
  downloadFile(
    `${safeFilename(report.title)}_${report.report_id}.md`,
    lines.join('\n'),
    'text/markdown',
  )
}

const PALETTE = ['#4f73d9', '#5fb3a1', '#e0a458', '#d16a6a', '#8f7fd1', '#69a8dc']

function fmtNum(value: unknown): string {
  const num = Number(value)
  if (!Number.isFinite(num)) return String(value ?? '')
  return num.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

/**
 * 把后端中立图表规格转换为 ECharts option。
 * 仅做结构映射，不做任何数据推断，数字全部来自后端 dataset。
 */
export function convertChartSpec(spec: NeutralChartSpec): Record<string, unknown> {
  const rows = spec.dataset ?? []
  switch (spec.type) {
    case 'pie': {
      const category = String((spec.encoding as { category?: string }).category ?? 'name')
      const valueField = spec.series[0]?.field ?? 'count'
      return {
        tooltip: { trigger: 'item' },
        legend: { bottom: 0 },
        series: [{
          type: 'pie',
          radius: ['38%', '66%'],
          data: rows.map((row) => ({ name: String(row[category] ?? ''), value: Number(row[valueField]) || 0 })),
        }],
      }
    }
    case 'scatter': {
      const enc = spec.encoding as { x?: string; y?: string; size?: string; label?: string }
      return {
        tooltip: { trigger: 'item' },
        grid: { left: 48, right: 16, top: 24, bottom: 32 },
        xAxis: { type: 'value', name: enc.x ?? '' },
        yAxis: { type: 'value', name: enc.y ?? '' },
        series: [{
          type: 'scatter',
          symbolSize: (data: number[]) => Math.max(8, Math.min(40, (data[2] ?? 1) * 20)),
          data: rows.map((row) => [
            Number(row[enc.x ?? 'x']) || 0,
            Number(row[enc.y ?? 'y']) || 0,
            Number(row[enc.size ?? 'size']) || 1,
            String(row[enc.label ?? 'label'] ?? ''),
          ]),
        }],
      }
    }
    case 'gauge': {
      const valueField = (spec.encoding as { value?: string }).value ?? 'risk_score'
      const labelField = (spec.encoding as { label?: string }).label ?? 'risk_level'
      const value = Number(rows[0]?.[valueField]) || 0
      const level = String(rows[0]?.[labelField] ?? '')
      return {
        tooltip: { formatter: `{b}: ${fmtNum(value)}（${level}）` },
        series: [{
          type: 'gauge',
          min: 0,
          max: 100,
          axisLine: { lineStyle: { width: 14, color: [[0.4, '#5fb3a1'], [0.7, '#e0a458'], [1, '#d16a6a']] } },
          pointer: { itemStyle: { color: 'auto' } },
          detail: { formatter: fmtNum(value) },
          data: [{ name: level || spec.title, value }],
        }],
      }
    }
    case 'kpi': {
      const labelField = (spec.encoding as { label?: string }).label ?? 'name'
      const valueField = (spec.encoding as { value?: string }).value ?? 'value'
      return {
        title: { text: rows.map((row) => `${row[labelField]}：${fmtNum(row[valueField])}`).join('　'), left: 'center', top: 'middle', textStyle: { fontSize: 14, fontWeight: 500 } },
      }
    }
    default: {
      // bar：encoding.category 可能为字符串或字符串数组（多维度拼接）
      const rawCat = (spec.encoding as { category?: string | string[] }).category
      const cats = Array.isArray(rawCat) ? rawCat : [String(rawCat ?? '')]
      const values = (spec.encoding as { values?: string[] }).values ?? spec.series.map((s) => s.field)
      return {
        tooltip: { trigger: 'axis' },
        legend: values.length > 1 ? { bottom: 0 } : undefined,
        grid: { left: 56, right: 16, top: 24, bottom: 40 },
        xAxis: {
          type: 'category',
          data: rows.map((row) => cats.map((col) => String(row[col] ?? '')).join(' / ')),
          axisLabel: { rotate: cats.length > 1 || rows.length > 6 ? 30 : 0, fontSize: 10 },
        },
        yAxis: { type: 'value' },
        series: values.map((field, index) => ({
          name: spec.series[index]?.name ?? field,
          type: 'bar',
          barWidth: '46%',
          data: rows.map((row) => Number(row[field]) || 0),
          itemStyle: { color: PALETTE[index % PALETTE.length], borderRadius: [4, 4, 0, 0] },
        })),
      }
    }
  }
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
  const requestId = newRequestId()

  const result = await assistantRequest<ChatResultContract>('/chat', {
    method: 'POST',
    signal: options.signal,
    body: JSON.stringify({
      message: options.message,
      session_id: options.sessionId,
      request_id: requestId,
    }),
  })

  const analysisResult = result.analysis?.result ?? null
  const summary = (
    (analysisResult?.summary_data
      ?? (analysisResult as { data?: unknown } | null)?.data
      ?? null) as Record<string, unknown> | null
  )

  return {
    reply: result.assistant_message.content || '（后端未返回文本内容）',
    status: result.status,
    sessionId: result.session_id,
    intentLabel: result.intent?.intent_label ?? result.intent?.intent ?? null,
    ...renderSummaryData(summary),
    warnings: result.warnings ?? [],
  }
}

/** 兼容旧页面组件的类型引用（不再使用本地规则兜底） */
export type { ChatMessage }
