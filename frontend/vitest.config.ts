import path from 'path';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// ----------------------------------------------------------------------

/**
 * Kept separate from `vite.config.ts` so tests never load `vite-plugin-checker`
 * (which would run eslint + tsc on every test run) or the dev proxy.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [{ find: /^src(.+)/, replacement: path.resolve(process.cwd(), 'src/$1') }],
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
});
