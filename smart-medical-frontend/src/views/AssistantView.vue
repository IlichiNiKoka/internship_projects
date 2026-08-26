<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import ChartCard from '../components/ChartCard.vue'
import {
  AssistantApiError,
  clearStoredSessionId,
  convertChartSpec,
  deleteAssistantSession,
  exportReportJson,
  exportReportMarkdown,
  fetchAssistantSession,
  generateAssistantReport,
  getAssistantReport,
  loadStoredSessionId,
  sendAssistantChat,
  storeSessionId,
} from '../api/chat'
import type { MedicalInsightReport } from '../api/chat'
import { useDashboardStore } from '../store/dashboard'
import type { ChatMessage } from '../types/dashboard'

const store = useDashboardStore()

const sessionId = ref<string | null>(loadStoredSessionId())
const restoring = ref(false)

const inputValue = ref('')
const sending = ref(false)
const abortController = ref<AbortController | null>(null)

const messages = ref<ChatMessage[]>([
  {
    id: 'welcome',
    role: 'assistant',
    title: '智能医疗助手',
    content:
      '你好，我是智慧医疗数据分析助手。你可以问我年龄结构、支付方式、重点病种、费用概览等问题，我会调用后端分析服务，基于 209 万条真实住院数据回答你。',
    createdAt: Date.now(),
    status: 'ok',
    source: 'backend-ai',
  },
])

const suggestedQuestions = [
  '按年龄组统计住院人次',
  '按支付方式统计住院人次',
  '按疾病类型统计住院人次',
  '平台数据总览',
  '疾病与支付方式的关联',
  '预测一个老年人急诊入院5天的费用',
  '哪些人群再入院风险高',
  '平台支持哪些分析',
]

const messageListRef = ref<HTMLElement | null>(null)

const activeReport = computed<ChatMessage | null>(() => {
  const list = [...messages.value].reverse()
  return list.find((message) => message.role === 'assistant' && message.chart) ?? null
})

const activeReportTitle = computed(() => activeReport.value?.title ?? '报告展示区')
const activeReportChart = computed<Record<string, unknown> | null>(() => activeReport.value?.chart ?? null)

const canSend = computed(() => inputValue.value.trim().length > 0 && !sending.value)

watch(
  () => messages.value.length,
  async () => {
    await nextTick()
    if (messageListRef.value) {
      messageListRef.value.scrollTop = messageListRef.value.scrollHeight
    }
  },
)

// ---------------------------------------------------------------------------
// 本地会话缓存：切界面由 KeepAlive 保活；刷新/关闭后从这里立即水合，
// 再与服务端对账，保证任何情况下历史都不丢失。
// ---------------------------------------------------------------------------
const CHAT_CACHE_KEY = 'assistant_chat_cache_v1'

interface ChatCachePayload {
  sessionId: string | null
  messages: ChatMessage[]
  reportId: string | null
}

function loadChatCache(): ChatCachePayload {
  try {
    const raw = localStorage.getItem(CHAT_CACHE_KEY)
    if (!raw) return { sessionId: null, messages: [], reportId: null }
    const parsed = JSON.parse(raw) as Partial<ChatCachePayload>
    return {
      sessionId: typeof parsed.sessionId === 'string' ? parsed.sessionId : null,
      messages: Array.isArray(parsed.messages) ? parsed.messages : [],
      reportId: typeof parsed.reportId === 'string' ? parsed.reportId : null,
    }
  } catch {
    return { sessionId: null, messages: [], reportId: null }
  }
}

function persistChatCache(reportId: string | null = insightReport.value?.report_id ?? null) {
  try {
    const payload: ChatCachePayload = {
      sessionId: sessionId.value,
      messages: messages.value.filter((item) => item.id !== 'welcome'),
      reportId,
    }
    localStorage.setItem(CHAT_CACHE_KEY, JSON.stringify(payload))
  } catch {
    // 存储配额满等异常不影响对话本身
  }
}

// ---------------------------------------------------------------------------
// 医疗洞察报告（结构化 JSON 报告渲染 + 导出）
// ---------------------------------------------------------------------------
const insightReport = ref<MedicalInsightReport | null>(null)
const reportLoading = ref(false)
const reportError = ref('')

const reportCharts = computed(() =>
  (insightReport.value?.charts ?? []).map((spec) => ({
    spec,
    option: convertChartSpec(spec) as Record<string, unknown>,
  })),
)

async function buildInsightReport() {
  if (!sessionId.value || reportLoading.value) return
  reportLoading.value = true
  reportError.value = ''
  try {
    insightReport.value = await generateAssistantReport(sessionId.value, {
      title: '多轮分析医疗洞察报告',
    })
    persistChatCache()
  } catch (error) {
    insightReport.value = null
    if (error instanceof AssistantApiError && error.status === 404) {
      reportError.value = '会话已过期或没有可用的分析结果，请先提问后再生成报告。'
    } else {
      reportError.value = error instanceof Error ? error.message : '报告生成失败，请稍后重试。'
    }
  } finally {
    reportLoading.value = false
  }
}

function handleExportJson() {
  if (insightReport.value) exportReportJson(insightReport.value)
}

function handleExportMarkdown() {
  if (insightReport.value) exportReportMarkdown(insightReport.value)
}

// ---------------------------------------------------------------------------
// 会话恢复：先本地缓存即时水合，再服务端对账（含结构化结果与最新报告联动）
// ---------------------------------------------------------------------------
onMounted(async () => {
  const stored = loadStoredSessionId()
  const cache = loadChatCache()
  // 1) 本地缓存优先：同一会话的历史消息与报告立即还原，无需等网络
  if (stored && cache.sessionId === stored && cache.messages.length) {
    sessionId.value = stored
    messages.value = [...messages.value, ...cache.messages]
    if (cache.reportId) {
      // 报告全文先用缓存里的 ID 占位，下面再从服务端拉完整数据
      getAssistantReport(stored, cache.reportId)
        .then((report) => {
          insightReport.value = report
        })
        .catch(() => {})
    }
  }
  if (!stored) return
  restoring.value = true
  try {
    const restored = await fetchAssistantSession(stored)
    sessionId.value = stored
    if (restored.messages.length) {
      // 服务端为权威版本，覆盖本地缓存的水合结果（含结构化图表/表格/KPI）
      messages.value = [
        ...messages.value.slice(0, 1),
        ...restored.messages.map((item) => ({
          id: crypto.randomUUID(),
          createdAt: item.createdAt,
          role: item.role,
          title: item.role === 'assistant' ? (item.intentLabel ?? '历史回答') : undefined,
          content: item.content,
          bullets: item.bullets.length ? item.bullets : undefined,
          table: item.table,
          chart: item.chart,
          kpis: item.kpis,
          status: item.status,
          source: 'backend-ai' as const,
        })),
      ]
    }
    // 联动最新洞察报告：本地缓存没有时从服务端拉取完整报告
    const latestReportId
      = insightReport.value?.report_id ?? restored.reportIds[restored.reportIds.length - 1] ?? null
    if (!insightReport.value && latestReportId) {
      const report = await getAssistantReport(stored, latestReportId)
      insightReport.value = report
    }
    persistChatCache()
  } catch (error) {
    // 服务端不可达但本地有缓存：保留已水合的历史，仅提示降级
    if (cache.sessionId === stored && cache.messages.length) {
      sessionId.value = stored
      return
    }
    clearStoredSessionId()
    sessionId.value = null
    if (error instanceof AssistantApiError && error.status !== 404) {
      messages.value.push({
        id: crypto.randomUUID(),
        createdAt: Date.now(),
        role: 'assistant',
        title: '会话恢复失败',
        content: '无法从服务器恢复上次会话，已为你准备新会话。',
        status: 'error',
      })
    }
  } finally {
    restoring.value = false
  }
})

function pushMessage(payload: Omit<ChatMessage, 'id' | 'createdAt'>) {
  messages.value.push({
    id: crypto.randomUUID(),
    createdAt: Date.now(),
    ...payload,
  })
  persistChatCache()
}

function formatTime(timestamp: number) {
  const date = new Date(timestamp)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`
}

async function chatOnce(prompt: string, sid: string | null, signal: AbortSignal) {
  try {
    const reply = await sendAssistantChat({ message: prompt, sessionId: sid, signal })
    sessionId.value = reply.sessionId
    storeSessionId(reply.sessionId)
    persistChatCache()
    return reply
  } catch (error) {
    if (error instanceof AssistantApiError && error.status === 404) {
      clearStoredSessionId()
      sessionId.value = null
      const reply = await sendAssistantChat({ message: prompt, sessionId: null, signal })
      sessionId.value = reply.sessionId
      storeSessionId(reply.sessionId)
      persistChatCache()
      return reply
    }
    throw error
  }
}

async function send(promptText?: string) {
  const prompt = (promptText ?? inputValue.value).trim()
  if (!prompt || sending.value) {
    return
  }

  pushMessage({ role: 'user', content: prompt, status: 'ok' })
  inputValue.value = ''
  sending.value = true

  abortController.value = new AbortController()

  try {
    const reply = await chatOnce(prompt, sessionId.value, abortController.value.signal)

    pushMessage({
      role: 'assistant',
      title: reply.intentLabel ?? '分析结果',
      content: reply.reply,
      bullets: reply.bullets,
      chart: reply.chart ?? null,
      table: reply.table ?? null,
      kpis: reply.kpis ?? null,
      status: reply.status === 'failed' ? 'error' : 'ok',
      source: 'backend-ai',
    })
  } catch (error) {
    const aborted = error instanceof DOMException && error.name === 'AbortError'
    const detail =
      error instanceof AssistantApiError ? `（状态 ${error.status}：${error.message}）` : ''
    pushMessage({
      role: 'assistant',
      title: aborted ? '已停止生成' : '请求失败',
      content: aborted
        ? '已停止当前回答。'
        : `无法连接后端 AI 服务${detail}，请确认后端已启动（python run.py）。`,
      status: 'error',
    })
  } finally {
    abortController.value = null
    sending.value = false
  }
}

async function startNewSession() {
  const previous = sessionId.value
  clearStoredSessionId()
  sessionId.value = null
  insightReport.value = null
  reportError.value = ''
  try {
    localStorage.removeItem(CHAT_CACHE_KEY)
  } catch { /* 忽略 */ }
  if (previous) {
    // 服务端删除旧会话；失败不阻塞新会话（TTL 会最终回收）
    deleteAssistantSession(previous).catch(() => {})
  }
  messages.value = [
    {
      id: 'welcome',
      role: 'assistant',
      title: '智能医疗助手',
      content: '已开启新会话。你可以问我年龄结构、支付方式、重点病种、费用概览等问题。',
      createdAt: Date.now(),
      status: 'ok',
      source: 'backend-ai',
    },
  ]
}

function stopGenerating() {
  abortController.value?.abort()
}

function retryLast() {
  const lastUser = [...messages.value].reverse().find((message) => message.role === 'user')
  if (lastUser) {
    send(lastUser.content)
  }
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void send()
  }
}
</script>

<template>
  <div class="chat-window">
    <section class="chat-conversation">
      <header class="chat-conversation-header">
        <div>
          <h2>智能医疗助手</h2>
          <p>
            {{ sending ? '正在生成回答…' : store.payload ? '已接入后端 AI 服务，可自然语言提问' : '平台数据加载中…' }}
          </p>
        </div>
        <div class="chat-header-actions">
          <button type="button" class="chat-send-button chat-send-button--ghost" @click="startNewSession">
            新会话
          </button>
          <span class="chat-source-badge" :data-live="sessionId != null">
            {{ sessionId ? `会话进行中 · ${sessionId.slice(0, 12)}…` : '新会话' }}
          </span>
        </div>
      </header>

      <div ref="messageListRef" class="chat-messages">
        <article v-for="message in messages" :key="message.id" class="chat-msg" :data-role="message.role" :data-status="message.status">
          <div class="chat-msg-avatar" :data-role="message.role">
            {{ message.role === 'user' ? '我' : 'AI' }}
          </div>
          <div class="chat-msg-body">
            <div class="chat-msg-meta">
              <strong>{{ message.role === 'user' ? '我' : '智能助手' }}</strong>
              <span>{{ formatTime(message.createdAt) }}</span>
              <span v-if="message.source" class="chat-msg-source">
                {{ message.source === 'backend-ai' ? '后端AI' : message.source === 'llm-api' ? '大模型 API' : '本地演示' }}
              </span>
            </div>
            <h4 v-if="message.title && message.role === 'assistant'">{{ message.title }}</h4>
            <p class="chat-msg-text">{{ message.content }}</p>
            <ul v-if="message.bullets?.length" class="chat-msg-bullets">
              <li v-for="bullet in message.bullets" :key="bullet">{{ bullet }}</li>
            </ul>
            <div v-if="message.kpis?.length" class="chat-msg-kpis">
              <span v-for="kpi in message.kpis" :key="kpi.label" class="chat-msg-kpi">
                <small>{{ kpi.label }}</small>
                <strong>{{ kpi.value }}</strong>
              </span>
            </div>
            <div v-if="message.table" class="chat-msg-table-wrap">
              <table class="chat-msg-table">
                <thead>
                  <tr>
                    <th v-for="col in message.table.columns" :key="col">{{ col }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, i) in message.table.rows" :key="i">
                    <td v-for="col in message.table.columns" :key="col">{{ row[col] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </article>

        <article v-if="sending" class="chat-msg" data-role="assistant">
          <div class="chat-msg-avatar" data-role="assistant">AI</div>
          <div class="chat-msg-body">
            <div class="chat-msg-meta">
              <strong>智能助手</strong>
              <span>正在输入</span>
            </div>
            <div class="chat-typing">
              <span></span><span></span><span></span>
            </div>
          </div>
        </article>
      </div>

      <div class="chat-suggestions">
        <button
          v-for="question in suggestedQuestions"
          :key="question"
          type="button"
          class="chat-chip"
          :disabled="sending"
          @click="send(question)"
        >
          {{ question }}
        </button>
      </div>

      <footer class="chat-composer">
        <textarea
          v-model="inputValue"
          class="chat-textarea"
          rows="2"
          placeholder="输入医疗分析问题，Enter 发送，Shift+Enter 换行"
          :disabled="sending"
          @keydown="handleKeydown"
        ></textarea>
        <div class="chat-composer-actions">
          <span class="chat-composer-hint">已接入后端 AI 编排层（意图识别 + 真实数据分析）</span>
          <button v-if="sending" type="button" class="chat-send-button chat-send-button--stop" @click="stopGenerating">
            停止
          </button>
          <button type="button" class="chat-send-button" :disabled="!canSend" @click="send()">
            发送
          </button>
        </div>
      </footer>
    </section>

    <aside class="chat-report">
      <header class="chat-report-header">
        <div>
          <h3>{{ insightReport?.title ?? activeReportTitle }}</h3>
          <p>{{ insightReport ? `生成于 ${new Date(insightReport.generated_at).toLocaleString('zh-CN')}` : '助手返回的可视化结果在这里渲染' }}</p>
        </div>
        <div class="chat-report-actions">
          <button
            type="button"
            class="chat-send-button"
            :disabled="!sessionId || reportLoading || sending"
            @click="buildInsightReport"
          >
            {{ reportLoading ? '生成中…' : (insightReport ? '刷新报告' : '生成洞察报告') }}
          </button>
          <button
            type="button"
            class="chat-send-button chat-send-button--ghost"
            :disabled="!insightReport"
            title="导出为 JSON 文件（与后端结构化契约一致）"
            @click="handleExportJson"
          >
            导出 JSON
          </button>
          <button
            type="button"
            class="chat-send-button chat-send-button--ghost"
            :disabled="!insightReport"
            title="导出为 Markdown 文件（含章节/指标/表格/来源）"
            @click="handleExportMarkdown"
          >
            导出 Markdown
          </button>
        </div>
      </header>

      <div v-if="reportError" class="chat-report-empty">
        <p>{{ reportError }}</p>
      </div>

      <!-- 结构化洞察报告 -->
      <template v-if="insightReport && !reportError">
        <div class="chat-report-body chat-insight">
          <p class="chat-insight-summary">{{ insightReport.executive_summary }}</p>

          <div v-if="insightReport.warnings.length" class="chat-insight-warnings">
            <span v-for="(warning, i) in insightReport.warnings" :key="i" class="chat-msg-source">
              ⚠ {{ warning.message ?? warning.code }}
            </span>
          </div>

          <section v-for="section in insightReport.sections" :key="section.section_id" class="chat-insight-section">
            <h4>{{ section.title }}</h4>
            <p class="chat-insight-query">{{ section.query }}</p>
            <p class="chat-insight-narrative" :data-trusted="section.summary_validation?.trusted !== false">
              {{ section.narrative }}
            </p>
            <div v-if="section.key_metrics.length" class="chat-msg-kpis">
              <span v-for="metric in section.key_metrics" :key="metric.key" class="chat-msg-kpi">
                <small>{{ metric.label }}</small>
                <strong>{{ Number(metric.value).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }}{{ metric.unit ? ` ${metric.unit}` : '' }}</strong>
              </span>
            </div>
            <div v-if="section.table" class="chat-msg-table-wrap">
              <table class="chat-msg-table">
                <thead>
                  <tr><th v-for="col in section.table.columns" :key="col">{{ col }}</th></tr>
                </thead>
                <tbody>
                  <tr v-for="(row, i) in section.table.rows" :key="i">
                    <td v-for="col in section.table.columns" :key="col">{{ row[col] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <template v-for="chart in reportCharts" :key="chart.spec.chart_id">
            <ChartCard :title="chart.spec.title" :option="chart.option" height="260px" />
          </template>
        </div>
      </template>

      <!-- 无报告时回退到最近一次对话图表 -->
      <template v-else-if="!reportError">
        <div v-if="activeReportChart" class="chat-report-body">
          <ChartCard title="分析图表" :option="activeReportChart" height="100%" />
        </div>
        <div v-else class="chat-report-empty">
          <div class="chat-report-empty-icon">📊</div>
          <p>向助手提问后，图表结果会展示在这里；多轮分析后可一键生成洞察报告。</p>
          <p class="chat-report-empty-sub">例如：点击上方“年龄结构如何？”</p>
        </div>
      </template>

      <div v-if="messages.some((message) => message.status === 'error')" class="chat-report-retry">
        <span>上一条回答失败</span>
        <button type="button" class="chat-send-button" @click="retryLast">重试</button>
      </div>
    </aside>
  </div>
</template>
