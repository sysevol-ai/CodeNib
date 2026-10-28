# CodeGraph peer readiness

Both selected peers can return current source after an external disk edit in
a small Python fixture without restarting MCP. This confirms an update-workflow
gap worth measuring: CodeNib's merged guard rejects stale sessions, but users
required rebuilding and reconnecting in those original checks. The later
[same-session reload regression](codegraph_reload_2026_10_27.json) recovers after
an explicit rebuild without reconnecting; automatic source-driven rebuilding
remains unimplemented. Original peer checks and matched-pilot observations are
unchanged. These checks do not establish task benefit.

The [retained receipts](codegraph_peers_2026_10_27.json) contain exact harnesses,
package identities, tool schemas, source hashes, setup failures, controller
mistakes and corrected requests. No model ran in these peer checks. The earlier
[SDK probes](codegraph_sdk_route.md) did use models; they remain separate.

## Observed behavior

| Product and actual mode | Check | Observed result |
| --- | --- | --- |
| Serena `2.1.0`, Python `3.13.18`, Pyright `1.1.403`, `claude-code` context, v2 REPL | Query a function, rename its callee on disk, then query source, the new definition and its references in the same MCP session | Current function body, new definition and correct caller returned without rebuilding/restarting |
| `colbymchenry/codegraph 1.6.2`, Linux arm64 bundle, direct single-writer MCP with watcher | Explore a function, rename its callee on disk, then explore again in the same session | Current source and new callee/caller relationship returned without manual synchronization |
| CodeNib source fixes #877–#879 | Separate [edit/rebuild/reconnect fixture](codegraph_currentness_2026_10_27.json) | Existing session rejects stale facts; explicit rebuild and a new session return current facts |

The peer fixture has one five-line file. Each peer uses its own temporary Git
repository. The controller waits one second after the edit; that is a test
condition, not a measured update latency. The CodeNib fixture is separate,
so this table cannot support a timing ratio or comparative accuracy score.

Serena was installed with an isolated uv tool environment and `SERENA_HOME`.
Its first index attempt failed because the isolated uv executable was absent
from the child PATH. Exposing that documented prerequisite allowed indexing.
The controller also initially mishandled MCP's `list_tools` result and used
an incorrect REPL expression. The corrected requests use `s.lsp` and an
expression result, as specified in the advertised tool description. Those
controller errors are preserved and do not count as product failures.

CodeGraph's npm launcher downloaded the pinned `1.6.2` bundle and reported a
verified release checksum. Telemetry is disabled. The original direct-mode
preflight uses `CODEGRAPH_NO_DAEMON=1` to avoid the real user's daemon registry.
The separate [arm64 runtime checks](codegraph_pilot_admission.md#native-arm64-runtime-checks)
now exercise documented project initialization and the default daemon-enabled
mode inside an owned container: source stays current in one MCP connection and
the background process remains after disconnect. Serena's REPL and CodeNib's
own-CLI rebuild/reconnect also pass there. Native CLI access is limited to the
selected product in each container; the host-only canary is absent. These are
readiness checks, not matched task timings. Neither peer's client-registration
installer ran against the user's real profile.

## Matched execution

The frozen policy uses these package versions and modes. Retain representative
source checks and degraded provider outcomes, including #871, in the cohort.
Capture each product's actual instructions and discovered
tools; Serena's recommended context contains explicit tool preferences.
Native read/search/edit/shell must remain available in every arm, and the
study prompt must not require CodeNib use.

The [task document](codegraph_matched_tasks_v1.json) and
[execution policy](codegraph_matched_policy_v3.json) now freeze membership,
acceptance, model/settings, ordering, limits, timeouts and usage/setup accounting
before matched inference. The [execution archive](codegraph_execution_2026_10_27.json)
binds the executable/runtime and retains representative query checks. The
[formal execution record](codegraph_matched_run_v1.md) now retains all twelve
workflows and native repair outcomes. The
[source assessment](codegraph_matched_assessment_v1.md) records insufficient
evidence under the original policy, including the criterion ambiguity and
material assistant explanation errors. Broader provider correctness and a valid
prospective benefit gate remain open. Small-fixture setup numbers and corrected warm retries
are not cold-start product evidence. The five external users
and seven-day observations remain separate gates in the
[pilot contract](codegraph_net_benefit.md).

Official configuration references checked: [Serena clients](https://oraios.github.io/serena/02-usage/030_clients.html),
[Serena REPL](https://oraios.github.io/serena/01-about/035_tools.html), and
[CodeGraph](https://github.com/colbymchenry/codegraph).
