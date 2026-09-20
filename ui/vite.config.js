import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const backendUrl = process.env.VITE_BACKEND_URL || "http://localhost:8000";

// The dev server forwards API calls to the backend, letting the front end hit
// same-origin paths like "/chat" — mirroring how nginx proxies things in prod.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/chat": { target: backendUrl, changeOrigin: true },
      "/health": { target: backendUrl, changeOrigin: true },
      "/session": { target: backendUrl, changeOrigin: true },
      "/feedback": { target: backendUrl, changeOrigin: true },
    },
  },
  build: { outDir: "dist" },
});
