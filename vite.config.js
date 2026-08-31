import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    host: '0.0.0.0',
    port: 5000,

    proxy: {
      // WebSocket connections -> Sys1 Python Load Balancer
      '/ws': {
        target: 'ws://127.0.0.1:6000',
        ws: true,
        changeOrigin: true,
      },

      // HTTP health endpoint -> Load Balancer
      '/health': {
        target: 'http://127.0.0.1:6000',
        changeOrigin: true,
      },

      // REST API -> Load Balancer
      '/api': {
        target: 'http://127.0.0.1:6000',
        changeOrigin: true,
      },

      // Load Balancer status
      '/lb': {
        target: 'http://127.0.0.1:6000',
        changeOrigin: true,
      },
    },
  },
});
