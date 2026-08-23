import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    host: '0.0.0.0',
    port: 5000,

    proxy: {
      // WebSocket connections — tunnelled through Load Balancer (port 8000)
      '/ws': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
        changeOrigin: true,
      },
      // Health & REST API — also routed through Load Balancer
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // Load Balancer status endpoint
      '/lb': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});
