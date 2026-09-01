import path from 'path';
import react from '@vitejs/plugin-react';
import checker from 'vite-plugin-checker';
import { defineConfig } from 'vite';

// ----------------------------------------------------------------------

const PORT = 5173;

/**
 * `VITE_DEV_API_TARGET` is where `/api/*` goes during development — the local
 * Python Worker started by `uv run pywrangler dev` at the repo root.
 *
 * In production there is no proxy and no build-time backend URL: one Worker
 * serves both the SPA assets and `/api/*` (see the root `wrangler.jsonc`), so
 * `CONFIG.serverUrl` stays empty and the client calls same-origin.
 */
const API_TARGET = process.env.VITE_DEV_API_TARGET ?? 'http://127.0.0.1:8787';

export default defineConfig({
  plugins: [
    react(),
    checker({
      // Dev-server only. During `vite build` the checker's eslint pass took 91%
      // of a 63s build; `pnpm check` runs tsc and eslint directly, and CI runs
      // them as separate steps, so doing it again inside the build is pure cost.
      enableBuild: false,
      typescript: true,
      eslint: {
        lintCommand: 'eslint "./src/**/*.{js,jsx,ts,tsx}"',
        useFlatConfig: true,
      },
      overlay: { position: 'tl', initialIsOpen: false },
    }),
  ],
  resolve: {
    alias: [{ find: /^src(.+)/, replacement: path.resolve(process.cwd(), 'src/$1') }],
  },
  server: {
    port: PORT,
    host: true,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
    },
  },
  preview: { port: PORT, host: true },
  build: {
    outDir: 'dist',
    // Stated explicitly rather than inherited: Vite 8 raised its own defaults
    // (Safari 16.0 -> 16.4, Firefox 104 -> 114, Chrome 107 -> 111) and a future
    // minor could move them again under us.
    target: ['chrome111', 'edge111', 'firefox114', 'safari16.4'],
    sourcemap: true,
    chunkSizeWarningLimit: 900,
  },
});
