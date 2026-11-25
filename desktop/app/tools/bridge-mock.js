// Stands in for the Tauri bridge when the window runs in a browser for checks.
// window.__MOCK_REPOS__ = [{ path, name, port, session }] names real
// `codenib wiki` services; `api` goes through tools/preview.py's proxy.
(() => {
  const repos = (window.__MOCK_REPOS__ || []).map((r) => ({
    path: r.path,
    name: r.name,
    analysis: true,
    generate: false,
    status: "ready",
    error: "",
    line: "",
    port: r.port,
    session: r.session,
  }));
  const handlers = {};
  const emit = (event, payload) => (handlers[event] || []).forEach((h) => h({ payload }));
  const view = (r) => ({ path: r.path, name: r.name, analysis: r.analysis, generate: r.generate, status: r.status, error: r.error, line: r.line });
  const commands = {
    setup_status: () => ({ codenib: "/Applications/CodeNib.app/Contents/Resources/python/bin/python3.12", version: "codenib 0.2.7", bundled: true }),
    repositories: () => repos.map(view),
    pick_folder: () => window.__MOCK_PICK__ || null,
    start_repository: ({ path }) => view(repos.find((r) => r.path === path)),
    restart_repository: ({ path }) => view(repos.find((r) => r.path === path)),
    add_repository: ({ path }) => {
      const known = repos.find((r) => r.path === path);
      if (known) return view(known);
      const added = { path, name: path.split("/").pop(), analysis: true, generate: false, status: "starting", error: "", line: "Auto preset: graph" };
      repos.push(added);
      setTimeout(() => emit("codenib://progress", { repo: path, line: "Views:" }), 200);
      setTimeout(() => emit("codenib://progress", { repo: path, line: "  bm25           fresh (0.41s)" }), 400);
      return view(added);
    },
    remove_repository: ({ path }) => {
      const i = repos.findIndex((r) => r.path === path);
      if (i >= 0) repos.splice(i, 1);
      return null;
    },
    open_external: () => null,
    show_in_finder: () => null,
    api: async ({ repo, method, path, body }) => {
      const r = repos.find((x) => x.path === repo);
      if (!r || !r.port) throw new Error("stopped: this repository is not open");
      const response = await fetch(`/__proxy/${r.port}${path}`, {
        method,
        headers: { "Content-Type": "application/json", "X-CodeNib-Session": r.session },
        body: body == null ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      if (!response.ok) {
        let detail = text;
        try {
          detail = JSON.parse(text).detail ?? text;
        } catch {}
        throw `HTTP ${response.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`;
      }
      return text ? JSON.parse(text) : null;
    },
  };
  window.__TAURI__ = {
    core: {
      invoke: async (cmd, args) => {
        const command = commands[cmd];
        if (!command) throw new Error(`no mock for ${cmd}`);
        return command(args || {});
      },
    },
    event: {
      listen: async (event, handler) => {
        (handlers[event] = handlers[event] || []).push(handler);
        return () => (handlers[event] = handlers[event].filter((h) => h !== handler));
      },
    },
  };
  window.__MOCK_EMIT__ = emit;
})();
