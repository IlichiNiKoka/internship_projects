<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { useDashboardStore } from './store/dashboard'

const router = useRouter()
const route = useRoute()
const store = useDashboardStore()

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
  <RouterView v-if="route.path === '/login'" />

  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand-card">
        <p class="brand-kicker">智慧医疗大数据平台</p>
        <h2>数据分析与可视化</h2>
        <p>参考“数据可视化设计”模板，聚焦关键数据展示。</p>
      </div>

      <nav class="nav-list">
        <RouterLink
          v-for="item in routeItems"
          :key="item.name"
          :to="item.path"
          class="nav-item"
          :class="{ 'is-active': route.path === item.path }"
        >
          <span class="nav-icon">{{ item.icon }}</span>
          <span>
            <strong>{{ item.title }}</strong>
            <small>{{ item.description }}</small>
          </span>
        </RouterLink>
      </nav>

      <section class="sidebar-footnote">
        <p class="sidebar-note-title">当前用户</p>
        <p class="sidebar-note-value">admin@qq.com</p>
        <div class="sidebar-note-row">
          <span class="mini-badge" :data-online="store.apiAvailable === true">
            {{ store.apiAvailable === true ? '在线 API 已连接' : '静态演示模式' }}
          </span>
          <button class="ghost-button ghost-button--sidebar" type="button" @click="router.push('/login')">
            退出
          </button>
        </div>
      </section>
    </aside>

    <main class="main-shell">
      <header class="topbar">
        <div>
          <h1>{{ currentTitle }}</h1>
          <p class="topbar-subtitle">基于清洗后医疗数据与分析服务接口输出进行展示</p>
        </div>
        <div class="topbar-actions">
          <button class="ghost-button" type="button" @click="store.loadStaticPayload">刷新静态数据</button>
        </div>
      </header>

      <section v-if="store.loading" class="empty-state">
        <h3>正在加载平台数据</h3>
        <p>首版前端会先读取汇总后的 JSON 数据，再渲染大屏与工作台。</p>
      </section>

      <section v-else-if="store.error" class="empty-state empty-state--error">
        <h3>数据加载失败</h3>
        <p>{{ store.error }}</p>
      </section>

      <RouterView v-else />
    </main>
  </div>
</template>
