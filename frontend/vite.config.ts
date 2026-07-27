import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes to web/dist and FastAPI serves it, so the demo stays a
// single process — no separate Node server to keep alive during a demo.
// `npm run dev` proxies the API to uvicorn instead, for hot reload.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../web/dist", emptyOutDir: true },
  server: {
    proxy: {
      "/agent": "http://127.0.0.1:8000",
      "/audit": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
});
