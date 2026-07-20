# Repository Guardian for Codex

In this Pier task, Guardian is also exposed through the filesystem so you can
use it even if MCP tool calls are unavailable.

Guardian runs in the background with its LLM enabled. It analyzes the initial
checkout and every new git commit, then writes its latest report here:

```bash
/app/.guardian/findings.md
/app/.guardian/findings.json
/app/.guardian/status.json
```

Read `/app/.guardian/findings.md` with your shell tool:

```bash
cat /app/.guardian/findings.md
```

Use it at these checkpoints:

1. Near the start of the task, after you inspect the repository.
2. After each commit or substantial refactor.
3. When tests fail in a way you did not expect.
4. Before finalizing, if you touched files Guardian marks as risky.

If `~/.guardian/status.json` says `"running": true`, use the current report
as the last completed analysis and continue. Guardian findings are advisory
context; verify them against the code and tests before acting.
