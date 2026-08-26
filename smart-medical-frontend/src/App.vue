<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { useDashboardStore } from './store/dashboard'

const router = useRouter()
const route = useRoute()
const store = useDashboardStore()

// 深色整页模式：可视化大屏路由进入时，把 body 切换为深色背景（离开时还原）
watch(
  () => route.meta.dark,
  (dark) => {
    document.body.classList.toggle('theme-dark-screen', Boolean(dark))
  },
  { immediate: true },
)

const routeItems = computed(() =>
  router.options.routes
    .filter((item) => item.name && !item.meta?.hideInNav)
    .map((item) => ({
      name: String(item.name),
      path: item.path,
      title: String(item.meta?.title ?? item.name),
      icon: String(item.meta?.icon ?? '•'),
      description: String(item.meta?.description ?? ''),
    })),
)

const currentTitle = computed(() => String(route.meta.title ?? '平台总览'))

onMounted(async () => {
  await store.init()
  // 探测后端在线分析服务并同步元数据（维度/指标/算法），
  // 使大屏等页面进入“在线 API 模式”
  await store.pingApi()
  await store.loadApiMeta()
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand-card">
        <span class="brand-logo">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v8M8 12h8" />
          </svg>
        </span>
        <span class="brand-text">
          <strong>智慧医疗大数据平台</strong>
          <small>数据分析与可视化</small>
        </span>
      </div>

      <nav class="nav-list">
        <RouterLink
          v-for="item in routeItems"
          :key="item.name"
          :to="item.path"
          class="nav-item"
          :class="{ 'is-active': route.path === item.path }"
        >
          <span class="nav-icon">
            <svg
              v-if="item.name === 'overview'"
              viewBox="0 0 24 24"
              width="20"
              height="20"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <rect x="3" y="3" width="7" height="7" rx="1.5" />
              <rect x="14" y="3" width="7" height="7" rx="1.5" />
              <rect x="14" y="14" width="7" height="7" rx="1.5" />
              <rect x="3" y="14" width="7" height="7" rx="1.5" />
            </svg>
            <svg
              v-else-if="item.name === 'screen'"
              viewBox="0 0 24 24"
              width="20"
              height="20"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <rect x="2" y="3" width="20" height="14" rx="2" />
              <path d="M8 21h8M12 17v4" />
            </svg>
            <svg
              v-else
              viewBox="0 0 24 24"
              width="20"
              height="20"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
            </svg>
          </span>
          <span class="nav-text">
            <strong>{{ item.title }}</strong>
            <small>{{ item.description }}</small>
          </span>
        </RouterLink>
      </nav>

      <div
        class="sidebar-status"
        :title="store.apiAvailable === true ? '当前为在线 API 实时分析模式' : '当前为静态演示模式（未连接后端）'"
      >
        <span class="api-status-dot" :class="store.apiAvailable === true ? 'is-online' : 'is-offline'"></span>
        <span class="status-text">{{ store.apiAvailable === true ? '在线 API 模式' : '静态演示模式' }}</span>
      </div>
    </aside>

    <main class="main-shell">
      <header class="topbar">
        <h1>{{ currentTitle }}</h1>
      </header>

      <section v-if="store.loading" class="empty-state">
        <h3>正在加载平台数据</h3>
        <p>首版前端会先读取汇总后的 JSON 数据，再渲染大屏与工作台。</p>
      </section>

      <section v-else-if="store.error" class="empty-state empty-state--error">
        <h3>数据加载失败</h3>
        <p>{{ store.error }}</p>
      </section>

      <RouterView v-else v-slot="{ Component }">
        <!-- KeepAlive：切换路由（总览/大屏/对话）时保留组件状态，
             对话历史与洞察报告不再因切界面丢失 -->
        <KeepAlive>
          <component :is="Component" />
        </KeepAlive>
      </RouterView>
    </main>
  </div>
</template>
