import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Vite only exposes VITE_-prefixed vars to client code via import.meta.env;
  // the config file itself runs in Node, so it needs loadEnv to read them.
  const env = loadEnv(mode, process.cwd(), '')

  // Defaults to localhost:8000 for host-run dev (npm run dev / make dev).
  // docker-compose overrides this to the backend service's name so the
  // containerized frontend doesn't try to resolve "localhost" to itself.
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || 'http://localhost:8000'

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: true,
      port: 5173,
      proxy: {
        '/api': {
          target: apiProxyTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
  }
})
