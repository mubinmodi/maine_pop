import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// In dev, the API runs separately on :8000 (`uv run synthpop serve`).
// Two pages: the landing page at / and the map at /map/.
export default defineConfig({
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000", "/geo": "http://127.0.0.1:8000" },
  },
  build: {
    outDir: "dist", chunkSizeWarningLimit: 2500,
    rollupOptions: {
      input: {
        landing: fileURLToPath(new URL("index.html", import.meta.url)),
        map: fileURLToPath(new URL("map/index.html", import.meta.url)),
      },
    },
  },
});
