<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()

const email = ref('admin@qq.com')
const password = ref('123456')
const remember = ref(true)

function submit() {
  // 演示用登录：直接进入总览页面
  router.push('/overview')
}
</script>

<template>
  <div class="login-shell">
    <div class="login-card">
      <div class="login-illustration" aria-hidden="true">
        <div class="login-illus-panel">
          <div class="login-illus-chip"></div>
          <div class="login-illus-chip login-illus-chip--alt"></div>
          <div class="login-illus-people">
            <span></span><span></span><span></span><span></span>
          </div>
        </div>
      </div>

      <form class="login-form" @submit.prevent="submit">
        <h1>智慧医疗数据分析与可视化</h1>
        <p>参考模板：居中卡片 + 圆角输入 + 主色按钮</p>

        <label class="login-field">
          <span>邮箱</span>
          <input v-model="email" type="email" autocomplete="username" />
        </label>

        <label class="login-field">
          <span>密码</span>
          <input v-model="password" type="password" autocomplete="current-password" />
        </label>

        <label class="login-remember">
          <input v-model="remember" type="checkbox" />
          <span>记住我</span>
        </label>

        <button class="login-button" type="submit">登录</button>
        <a class="login-link" href="#" @click.prevent>还没有账号？立即注册！</a>
      </form>
    </div>
  </div>
</template>

