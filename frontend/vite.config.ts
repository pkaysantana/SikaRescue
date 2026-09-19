import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API is a single-process FastAPI server (scripts/serve_demo.py). In development the
// Vite server proxies /api to it, so the browser only ever talks to one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
