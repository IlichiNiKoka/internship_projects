import { createRouter, createWebHashHistory } from 'vue-router'

import AssistantView from '../views/AssistantView.vue'
import BigScreenView from '../views/BigScreenView.vue'
import LoginView from '../views/LoginView.vue'
import OverviewView from '../views/OverviewView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/',
      redirect: '/login',
    },
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: {
        title: '用户登录',
        icon: '⎆',
        description: '平台入口（演示用）',
        hideInNav: true,
      },
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
