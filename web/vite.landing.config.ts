// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
// SPDX-License-Identifier: Apache-2.0

// Builds the codenib.ai hero map (landing-hero/main.tsx) into
// ../landing/assets/hero-map/, a self-contained module and stylesheet the
// static landing page loads from its own origin. An app build rather than
// library mode, so the output is fully minified for browsers.
import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname),
    },
  },
  build: {
    outDir: path.resolve(__dirname, "../landing/assets/hero-map"),
    emptyOutDir: true,
    copyPublicDir: false,
    sourcemap: false,
    modulePreload: false,
    rollupOptions: {
      input: path.resolve(__dirname, "landing-hero/main.tsx"),
      output: {
        entryFileNames: "hero-map.js",
        chunkFileNames: "[name]-[hash].js",
        // The page links the entry stylesheet by name; chunks keep hashes.
        assetFileNames: (asset) =>
          asset.names.includes("main.css") ? "hero-map.css" : "[name]-[hash][extname]",
      },
    },
  },
});
