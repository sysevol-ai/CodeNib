# Repository Guardian for Codex

In this Pier task, Guardian is also exposed through the filesystem so you can
use it even if MCP tool calls are unavailable.

Guardian runs in the background with its LLM enabled. It analyzes the initial
checkout and every new git commit, then writes its latest report here:

```bash
~/.guardian/findings.md
~/.guardian/findings.json
~/.guardian/status.json
```

The synchronized checkpoint command is:

```bash
~/.guardian/bin/guardian-checkpoint
```

This command waits until Guardian has finished the report for the current
`HEAD`, prints the Guardian model/backend/token summary, then prints the full
report. If it fails or times out, do not silently ignore it; inspect
`~/.guardian/status.json` and explain the failure before finalizing.

Read `~/.guardian/findings.md` with your shell tool:

```bash
cat ~/.guardian/findings.md
```

Use it at these checkpoints:

1. Near the start of the task, after you inspect the repository.
2. After each commit or substantial refactor.
3. When tests fail in a way you did not expect.
4. Before finalizing, always run:

   ```bash
   ~/.guardian/bin/guardian-checkpoint
   ```

   If it reports findings for the current commit, address them with another
   edit/commit or explicitly explain why no code change is needed. Do not give
   your final answer until this checkpoint has succeeded for the current
   `HEAD`.

If `~/.guardian/status.json` says `"running": true`, use the current report
as the last completed analysis and continue during normal work. At final
handoff, use `~/.guardian/bin/guardian-checkpoint` so you read the fresh report
for the current commit. Guardian findings are advisory context; verify them
against the code and tests before acting.
