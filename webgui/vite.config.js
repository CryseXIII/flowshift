import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync, existsSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
let devPort = 5173

try {
  const cfgPath = resolve(__dirname, 'config.json')
  if (existsSync(cfgPath)) {
    const cfg = JSON.parse(readFileSync(cfgPath, 'utf-8'))
    if (cfg.port) devPort = parseInt(cfg.port, 10)
  }
} catch {}

// Stamp every build with the product VERSION so the installer and the runtime
// can tell whether the deployed WebGUI belongs to the installed program.
function flowshiftVersionStamp() {
  return {
    name: 'flowshift-version-stamp',
    apply: 'build',
    generateBundle() {
      const versionPath = resolve(__dirname, '..', 'VERSION')
      if (!existsSync(versionPath)) return
      const version = readFileSync(versionPath, 'utf-8').trim()
      if (version) this.emitFile({ type: 'asset', fileName: 'VERSION', source: `${version}\n` })
    },
  }
}

export default defineConfig({
  plugins: [react(), flowshiftVersionStamp()],
  server: {
    port: devPort,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        overlay: resolve(__dirname, 'overlay.html'),
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.js',
    pool: 'threads',
  },
})
