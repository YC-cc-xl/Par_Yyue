import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Vite 配置：仅编译渲染进程（Vue3）
// 主进程/预加载脚本为独立的 CJS 文件，不经过 Vite 打包
export default defineConfig({
  plugins: [vue()],
  // Electron 生产环境使用 file:// 加载，必须用相对路径
  base: './',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    port: 5173,
    strictPort: true
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
})
