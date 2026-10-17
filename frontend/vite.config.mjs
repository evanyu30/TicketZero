import { defineConfig, loadEnv } from 'vite';

// The page calls /api/*; Vite forwards it to the TicketZero backend. BACKEND_URL comes from the shell or the
// project root .env (shared with the backend), default http://127.0.0.1:8000.
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, '..', ''), ...process.env };
  const proxy = { '/api': { target: env.BACKEND_URL || 'http://127.0.0.1:8000', changeOrigin: true, rewrite: p => p.replace(/^\/api/, '') } };
  return { server: { host: '127.0.0.1', proxy }, preview: { host: '127.0.0.1', proxy } };
});
