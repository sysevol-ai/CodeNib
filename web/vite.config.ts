import { defineConfig, searchForWorkspaceRoot, type Plugin } from "vite";
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

// `codenib wiki` serves one machine's repositories: tell the page so its home
// is those repositories rather than the hosted landing. The hosted demo never
// sets this variable and keeps public/runtime-config.js as it is.
function localWikiRuntime(): Plugin {
  return {
    name: "codenib-local-wiki-runtime",
    configureServer(server) {
      if (process.env.CODENIB_LOCAL_WIKI !== "1") return;
      server.middlewares.use((request, response, next) => {
        if (request.url?.split("?")[0] !== "/runtime-config.js") return next();
        response.setHeader("Content-Type", "text/javascript; charset=utf-8");
        response.setHeader("Cache-Control", "no-store");
        response.end(
          'window.__CODENIB_RUNTIME__ = Object.freeze({ mode: "api", basePath: "/", local: true });\n' +
            'window.__CODENIB_API_BASE__ = "";\n',
        );
      });
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [react(), localWikiRuntime()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname),
    },
  },
  server: {
    allowedHosts,
    proxy: apiProxy,
    fs: {
      allow: [searchForWorkspaceRoot(__dirname), path.resolve(__dirname, "../landing/assets/explore")],
    },
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
