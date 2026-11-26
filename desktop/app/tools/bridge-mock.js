// Stands in for the Tauri bridge when the window runs in a browser for checks.
// window.__MOCK_REPOS__ = [{ path, name, port, session }] names real
// `codenib wiki` services; `api` goes through tools/preview.py's proxy.
(() => {
  const repos = (window.__MOCK_REPOS__ || []).map((r) => ({
    path: r.path,
    name: r.name,
    analysis: true,
    generate: false,
    writer: null,
    remote: r.remote || null,
    status: "ready",
    error: "",
    line: "",
    port: r.port,
    session: r.session,
  }));
  const handlers = {};
  const emit = (event, payload) => (handlers[event] || []).forEach((h) => h({ payload }));
  const view = (r) => ({
    path: r.path,
    name: r.remote || r.name,
    analysis: r.analysis,
    generate: !!r.writer,
    writer: r.writer || null,
    remote: r.remote || null,
    status: r.status,
    error: r.error,
    line: r.line,
  });
  // Accounts: window.__MOCK_ACCOUNTS__ may override any of these.
  const accounts = Object.assign(
    {
      agents: [
        { id: "claude-code", name: "Claude Code", installed: true, signed_in: true, detail: "Claude Max plan", path: "/usr/local/bin/claude", install: "curl -fsSL https://claude.ai/install.sh | bash", docs: "https://code.claude.com/docs/en/setup" },
        { id: "codex", name: "Codex", installed: false, signed_in: false, detail: null, path: null, install: "npm install -g @openai/codex", docs: "https://developers.openai.com/codex/cli" },
      ],
      github: { signed_in: true, login: "octocat", name: "The Octocat", source: "git", device_sign_in: false, problem: null },
      mine: [
        { full_name: "octocat/Hello-World", description: "My first repository on GitHub!", private: false, pushed_at: "2026-11-20T10:00:00Z", language: null },
        { full_name: "octocat/private-notes", description: "Notes", private: true, pushed_at: "2026-11-24T10:00:00Z", language: "Python" },
      ],
    },
    window.__MOCK_ACCOUNTS__ || {},
  );
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
    agents_status: () => accounts.agents,
    agent_sign_in: () => null,
    agent_install: () => null,
    github_status: () => accounts.github,
    github_use_token: ({ token }) => {
      if (!token.startsWith("ghp_")) throw "GitHub rejected this token (it may have expired or been revoked)";
      accounts.github = { signed_in: true, login: "octocat", name: null, source: "codenib", device_sign_in: false, problem: null };
      return accounts.github;
    },
    github_sign_out: () => (accounts.github = { signed_in: false, login: null, name: null, source: null, device_sign_in: false, problem: null }),
    github_device_start: () => { throw "GitHub sign-in is not configured in this build"; },
    github_device_wait: () => accounts.github,
    github_device_cancel: () => null,
    github_repositories: () => {
      if (!accounts.github.signed_in) throw "Sign in to GitHub to list your repositories";
      return accounts.mine;
    },
    github_lookup: ({ input }) => {
      const m = /([\w.-]+)\/([\w.-]+?)(\.git)?\/?$/.exec(input.trim().replace(/^https?:\/\/github\.com\//, ""));
      if (!m) throw "Enter a GitHub repository as owner/repo or its github.com address";
      const slug = `${m[1]}/${m[2]}`;
      const known = accounts.mine.find((r) => r.full_name === slug);
      return {
        repository: known || { full_name: slug, description: null, private: false, pushed_at: null, language: null },
        destination: `/Users/me/CodeNib/${slug}`,
        present: false,
      };
    },
    clone_repository: async ({ input }) => {
      for (const line of ["Cloning into '/Users/me/CodeNib/x'...", "Receiving objects:  52% (120/230)", "Resolving deltas: 100% (80/80), done."]) {
        emit("codenib://clone", { slug: input, line });
        await new Promise((r) => setTimeout(r, 150));
      }
      return window.__MOCK_CLONE_TO__ || `/Users/me/CodeNib/${input}`;
    },
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
