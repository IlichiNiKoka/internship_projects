import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // 开发环境把 /api 前缀的请求转发到本地 Flask 分析服务（后端未开启 CORS，
    // 必须通过同源代理访问；生产环境应由 Nginx/BFF 做同样的转发）
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
    },
  },
})
