import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import path from 'node:path'

// dev 期 /agent /workspace /health 由 Vite proxy 转发到 FastAPI:8000（生产由 FastAPI 直接托管）
// 生产构建挂在 FastAPI /workspace 下，静态资源 base 相应调整
export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/workspace/' : '/',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    port: 5173,
    proxy: {
      // 显式 127.0.0.1：本机 localhost 可能优先解析 ::1，而该地址被 WSL relay 占用
      '/agent': 'http://127.0.0.1:8000',
      '/workspace': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
}))
