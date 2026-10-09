import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // @ts-expect-error I don't want to install @types/node for this one line
    minify: process.env.NO_MINIFY !== 'true',
  },
  server: {
    proxy: {
      // Route /mcp-proxy/* through the local FastAPI backend to bypass CORS.
      // timeout/proxyTimeout must be 0 (no limit): an MCP SSE stream is idle for
      // as long as the server has nothing to send, and the default timeouts would
      // kill a healthy long-lived session mid-connection.
      '/mcp-proxy': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  },
})

