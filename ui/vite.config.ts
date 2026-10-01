import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into ../app/static/ui and served by the API at /ui/ (same origin: no CORS).
// `npm run dev` proxies /api to a running API (default http://localhost:8080).
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  build: {
    outDir: "../app/static/ui",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: { react: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"] },
      },
    },
  },
  server: {
    proxy: { "/api": process.env.API_URL ?? "http://localhost:8080" },
  },
});
