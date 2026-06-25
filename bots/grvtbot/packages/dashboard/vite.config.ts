/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';

// Vite config for the GRVT Grid dashboard.
// - React 19 + Tailwind v4 plugin (no PostCSS config needed)
// - Path alias `@/*` → `src/*`
// - Dev proxy: /api/* and /ws → backend at localhost:3848 (override via env)
//   This avoids CORS issues during development without touching the backend.
export default defineConfig({
  // The bot's express server mounts the SPA under /dashboard/. All asset
  // URLs in index.html need to be prefixed accordingly so the browser
  // resolves them against /dashboard/assets/* (not /assets/*).
  // Override via VITE_BASE_PATH=/ for local dev or alternative deployments.
  base: process.env.VITE_BASE_PATH ?? '/dashboard/',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rollupOptions: {
      output: {
        // Split heavy chart libs into their own vendor chunks so the
        // initial app shell stays small. The browser caches them
        // separately and the wizard/detail pages lazy-load them.
        manualChunks: {
          'vendor-react': ['react', 'react-dom', 'react-router-dom'],
          'vendor-charts-lwc': ['lightweight-charts'],
          'vendor-charts-recharts': ['recharts'],
          'vendor-query': ['@tanstack/react-query'],
        },
      },
    },
    chunkSizeWarningLimit: 600,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_TARGET ?? 'http://localhost:3848',
        changeOrigin: true,
      },
      '/ws': {
        target: (process.env.VITE_BACKEND_TARGET ?? 'http://localhost:3848').replace(
          /^http/,
          'ws'
        ),
        ws: true,
        changeOrigin: true,
      },
    },
  },
  test: {
    // jsdom gives us a DOM in node so @testing-library/react can render.
    // globals: true exposes describe/it/expect without per-file import,
    // matching the bot/notifier convention.
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    css: false, // tailwind compilation isn't worth it in tests
  },
});
