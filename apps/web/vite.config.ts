import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

// Same path as nginx in the container: /api/* goes to the FastAPI service.
const apiTarget = process.env.ENIGRAM_API_URL ?? 'http://127.0.0.1:8080'
// Like nginx: the key is added by the proxy, never shipped to the browser.
const apiKey = process.env.ENIGRAM_WEB_KEY

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: {
      '/api': {
        target: apiTarget,
        rewrite: (path) => path.replace(/^\/api/, ''),
        headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : undefined,
      },
    },
  },
  test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'] },
})
