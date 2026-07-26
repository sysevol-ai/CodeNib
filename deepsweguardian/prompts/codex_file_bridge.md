# Repository Guardian for Codex

In this Pier task, Guardian is also exposed through the filesystem so you can
use it even if MCP tool calls are unavailable.

Guardian is initially stopped. Start it explicitly with:

```bash
/app/.guardian/bin/guardian-start
```

This action is idempotent. It uses the initial checkout recorded during setup
as cycle 0 without spending model tokens, then analyzes only commits you make.
The synchronized checkpoint command also starts Guardian automatically if
needed.

Once started, Guardian writes its latest report here:

```bash
/app/.guardian/out/findings.md
/app/.guardian/out/findings.json
/app/.guardian/out/status.json
```

The synchronized checkpoint command is:

```bash
/app/.guardian/bin/guardian-checkpoint
```

This command starts Guardian if needed, waits until it has finished the report
for the current `HEAD`, prints the Guardian model/backend/token summary, then
prints the full report. If `HEAD` is still the initial baseline it exits without
running an analysis. If it otherwise fails or times out, do not silently ignore
it; inspect `/app/.guardian/out/status.json` and explain the failure before
finalizing.

Read `/app/.guardian/out/findings.md` with your shell tool:

```bash
cat /app/.guardian/out/findings.md
```

Use it at these checkpoints:

1. Near the start of the task, after you inspect the repository, run
   `guardian-start` to begin monitoring without analyzing the baseline.
2. After each commit or substantial refactor.
3. When tests fail in a way you did not expect.
4. Before finalizing, always run:

   ```bash
   /app/.guardian/bin/guardian-checkpoint
   ```

   Address verified findings and high-confidence backlog items with another
   edit/commit, or explicitly explain why no code change is needed. If
   `analysis status` is degraded, do not describe zero findings as a clean
   review: perform the missing validation yourself and explain the limitation.
   Do not give your final answer until this checkpoint has succeeded for the
   current `HEAD`.

If `/app/.guardian/out/status.json` says `"running": true`, use the current report
as the last completed analysis and continue during normal work. At final
handoff, use `/app/.guardian/bin/guardian-checkpoint` so you read the fresh report
for the current commit. Guardian findings are advisory context; verify them
against the code and tests before acting.
