import { createRouter, createWebHashHistory } from 'vue-router'

// 路由级代码分割：各页面按需加载。
// 可视化大屏依赖完整 echarts（体积较大），改为动态 import 后只在进入大屏时加载，
// 首屏 bundle 显著减小、加载更快（build 产物按页面拆分 chunk）。
const OverviewView = () => import('../views/OverviewView.vue')
const BigScreenView = () => import('../views/BigScreenView.vue')
const AssistantView = () => import('../views/AssistantView.vue')

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      redirect: '/overview',
    },
    {
      path: '/overview',
      name: 'overview',
      component: OverviewView,
      meta: {
        title: '数据总览',
        icon: '◫',
        description: 'KPI 与关键可视化',
      },
    },
    {
      path: '/screen',
      name: 'screen',
      component: BigScreenView,
      meta: {
        title: '可视化大屏',
        icon: '▣',
        description: '深色大屏与筛选联动',
        dark: true,
      },
    },
    {
      path: '/assistant',
      name: 'assistant',
      component: AssistantView,
      meta: {
        title: '智能对话',
        icon: '✦',
        description: '对话窗口与大模型接入',
      },
    },
  ],
})

export default router
