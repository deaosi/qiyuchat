import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: Object.fromEntries(['/api', '/login', '/logout'].map(path => [path, {
      target: loadEnv(mode, process.cwd(), '').QIYU_API_PROXY || process.env.QIYU_API_PROXY || 'http://127.0.0.1:8848',
      changeOrigin: true,
    }])),
  },
}))
