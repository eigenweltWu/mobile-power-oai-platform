import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The FastAPI backend mounts the built dist/ at '/', so all assets must be
// resolved from the root origin. API calls stay relative (/api/...).
export default defineConfig({
  base: '/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
});
