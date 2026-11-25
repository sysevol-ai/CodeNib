import path from "node:path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Tauri serves the built files from the app bundle; the dev server is only
// for `tauri dev`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: { port: 5173, strictPort: true },
  build: { target: "safari15", outDir: "dist", emptyOutDir: true },
});
