import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') }
  },
  server: {
    proxy: {
      '/api':    { target: 'http://localhost:8000', changeOrigin: true },
      '/images': { target: 'http://localhost:8000', changeOrigin: true },
      '/pdf':    { target: 'http://localhost:8000', changeOrigin: true },
    }
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        manualChunks: {
          pdfjs: ['pdfjs-dist'],
          elementplus: ['element-plus'],
        }
      }
    }
  },
  optimizeDeps: {
    include: ['pdfjs-dist']
  }
})
