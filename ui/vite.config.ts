import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The build lands in the Python package and is committed: the server and the Docker image need no Node.
// The page's CSP is 'self' only (web/server.py), so nothing may be inlined: no data: assets, no preload polyfill.
// Dev: `npm run dev` proxies /api to a running console (SHORTBOT_API, default the local one on :8080).
export default defineConfig({
  plugins: [react()],
  base: '/static/',
  build: {
    outDir: '../src/shortbot/web/static',
    emptyOutDir: true,
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
  },
  server: { proxy: { '/api': process.env.SHORTBOT_API ?? 'http://127.0.0.1:8080' } },
})
