// vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Proxy all /api/* and /ws/* to FastAPI during development
      "/api": { target: "http://localhost:8000", rewrite: (p) => p.replace(/^\/api/, "") },
      "/ws":  { target: "ws://localhost:8000",   ws: true },
    },
  },
  build: {
    // In local dev: build to dist/ (Docker copies dist/ → yukti/api/static/)
    // To build directly into FastAPI: npm run build:fastapi
    outDir:    "dist",
    emptyOutDir: true,
  },
});
