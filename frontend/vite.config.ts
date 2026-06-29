import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server is reachable from other devices (phone) on the LAN via host: true.
// /api is proxied to the FastAPI backend so the frontend can use relative URLs.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
