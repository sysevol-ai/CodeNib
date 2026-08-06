# Repository Guardian for Codex

In this Pier task, Guardian is also exposed through the filesystem so you can
use it even if MCP tool calls are unavailable.

Guardian's host controller starts with the solver. Confirm that its exchange is
ready with:

```bash
/app/.guardian/bin/guardian-start
```

You may voluntarily send Guardian your current understanding or a concern
before committing:

```bash
printf '%s' 'I believe this change adds X; I am least certain about Y.' |
  /app/.guardian/bin/guardian-message --scope path/to/file.py
```

This does not start or interrupt a review. The message is untrusted context,
not evidence, and is snapshotted by the next commit cycle. Do not forward the
benchmark instruction mechanically; communicate your own useful understanding.

This action is idempotent. The initial checkout recorded during setup is cycle
0 and spends no model tokens. Each checkpoint publishes an exact Git bundle;
Guardian analyzes it in separate disposable sandboxes.

Guardian publishes its latest report here:

```bash
/logs/agent/guardian_exchange/latest/findings.md
/logs/agent/guardian_exchange/latest/findings.json
/logs/agent/guardian_exchange/latest/status.json
```

The synchronized checkpoint command is:

```bash
/app/.guardian/bin/guardian-checkpoint
```

This command publishes the current commit, waits until the host controller has
finished its report, prints the Guardian model/backend/token summary, then
prints the full report. If `HEAD` is still the initial baseline it exits without
running an analysis. If it otherwise fails or times out, do not silently ignore
it; inspect the matching response under
`/logs/agent/guardian_exchange/responses/` and explain the failure before
finalizing.

Read the latest completed report with your shell tool:

```bash
cat /logs/agent/guardian_exchange/latest/findings.md
```

Use it at these checkpoints:

1. Near the start of the task, after you inspect the repository, run
   `guardian-start` to confirm the exchange without analyzing the baseline.
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

   A report marked `terminal: true` is the last Guardian review allowed for
   this task. Verify its remaining findings yourself. You may edit and test the
   repository afterward, but a later checkpoint will acknowledge that the new
   commit is unreviewed and will not invoke Guardian again. Finish the task
   after that acknowledgement instead of seeking another review cycle.

At final handoff, use `/app/.guardian/bin/guardian-checkpoint` so you read the
fresh report for the current commit. Guardian findings are advisory context;
verify them against the code and tests before acting.
