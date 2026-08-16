import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

const apiBase = process.env.CODENIB_API_BASE || "http://127.0.0.1:8000";
// Keep Vite's DNS-rebinding protection enabled while admitting the public proxy.
const allowedHosts = ["demo.codenib.ai"];
const apiProxy = {
  "/api": {
    target: apiBase,
    changeOrigin: true,
  },
};

export default defineConfig({
  base: "./",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname),
    },
  },
  server: {
    allowedHosts,
    proxy: apiProxy,
  },
  preview: {
    allowedHosts,
    proxy: apiProxy,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
