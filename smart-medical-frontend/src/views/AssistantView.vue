<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import ChartCard from '../components/ChartCard.vue'
import {
  AssistantApiError,
  clearStoredSessionId,
  loadStoredSessionId,
  sendAssistantChat,
  storeSessionId,
} from '../api/chat'
import { useDashboardStore } from '../store/dashboard'
import type { ChatMessage } from '../types/dashboard'

const store = useDashboardStore()

const sessionId = ref<string | null>(loadStoredSessionId())

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

function pushMessage(payload: Omit<ChatMessage, 'id' | 'createdAt'>) {
  messages.value.push({
    id: crypto.randomUUID(),
    createdAt: Date.now(),
    ...payload,
  })
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
    return reply
  } catch (error) {
    if (error instanceof AssistantApiError && error.status === 404) {
      clearStoredSessionId()
      sessionId.value = null
      const reply = await sendAssistantChat({ message: prompt, sessionId: null, signal })
      sessionId.value = reply.sessionId
      storeSessionId(reply.sessionId)
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

function startNewSession() {
  clearStoredSessionId()
  sessionId.value = null
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
          <h3>{{ activeReportTitle }}</h3>
          <p>助手返回的可视化结果在这里渲染</p>
        </div>
      </header>

      <div v-if="activeReportChart" class="chat-report-body">
        <ChartCard title="分析图表" :option="activeReportChart" height="100%" />
      </div>
      <div v-else class="chat-report-empty">
        <div class="chat-report-empty-icon">📊</div>
        <p>向助手提问后，图表结果会展示在这里。</p>
        <p class="chat-report-empty-sub">例如：点击上方“年龄结构如何？”</p>
      </div>

      <div v-if="messages.some((message) => message.status === 'error')" class="chat-report-retry">
        <span>上一条回答失败</span>
        <button type="button" class="chat-send-button" @click="retryLast">重试</button>
      </div>
    </aside>
  </div>
</template>
