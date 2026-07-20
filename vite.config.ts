import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Pinned so the dev origin always matches the backend CORS allowlist
    // (a drifting port like 5174 would be silently blocked by CORS).
    port: 5173,
    strictPort: true,
  },
})
