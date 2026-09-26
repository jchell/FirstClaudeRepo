import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  build: {
    // The lazily loaded graph chunk (ELK layout) is ~1.6 MB; it only loads on lineage pages.
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        // Keep the graph libraries (lineage views) out of the main bundle.
        manualChunks: { graph: ['@xyflow/react', 'elkjs/lib/elk.bundled.js'], mantine: ['@mantine/core', '@mantine/hooks'] },
      },
    },
  },
  server: {
    port: 3000,
    // Dev server proxies the API like nginx does in the container, so the refresh
    // cookie stays same-origin.
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
});
