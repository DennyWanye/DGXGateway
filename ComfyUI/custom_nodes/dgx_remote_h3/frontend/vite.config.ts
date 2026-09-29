import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath, URL } from 'node:url';
export default defineConfig({
  base: './', plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: { outDir: '../web/studio', emptyOutDir: true, rollupOptions: { output: { entryFileNames: 'assets/[name]-[hash].mjs', chunkFileNames: 'assets/[name]-[hash].mjs' } } },
  server: {
    proxy: {
      '/dgx-drama': {
        target: 'http://127.0.0.1:18189',
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            // The in-app preview origin is this dev server. ComfyUI rejects
            // loopback writes whose Origin port does not match Host.
            proxyReq.setHeader('Origin', 'http://127.0.0.1:18189');
          });
        },
      },
    },
  },
});
