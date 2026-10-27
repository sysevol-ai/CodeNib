# CodeGraph real-workflow net-benefit pilot

Owner: CodeNib product and CodeGraph maintainers.
Status: offline workflow reproduction and a dirty-initialization candidate
verified; no pilot inference or external trial has run. Established 2026-10-26
(Pacific); target review 2026-11-09.
Current outcomes and open gates belong to [adoption A0](../adoption_roadmap.md).
Tracking issue: [#874](https://github.com/sysevol-ai/CodeNib/issues/874).

## Product question and decision

Can a developer complete source-grounded coding tasks more effectively with
CodeGraph after paying its setup, indexing and update costs? The intended
initial use case is cross-file execution-path and change-impact analysis.
This pilot produces a promote, narrow, no-go or insufficient-evidence decision.
It does not promise a positive result or establish population-wide efficacy.

The pilot product gate is at least 20% improvement on one prospectively
selected endpoint, complete task time or recorded model usage, against the
strongest eligible alternative. Task acceptance must not fall; any additional
serious correctness error blocks promotion. Fixing a recorded issue or
publishing this plan does not satisfy that gate.

## Starting evidence

The 2026-10-26 source-contract audit covers main `5f0b7e9d`:

- `codenib/cli.py` rejects a non-clean checkout during standard CodeGraph
  initialization; `docs/codegraph.md` asks users to rerun initialization after
  changes and states that file-level delta repair is disabled. These are
  source/doc observations, not new runtime measurements.
- The recorded Requests session disables native tools and enables two CodeNib
  tools. It demonstrates source retrieval and a multi-hop query, not natural
  tool adoption or a matched task benefit.
- The submitted ContextBench route does not use graph expansion. ARB's fixed
  graph-expansion ranking is a separate condition from an interactive product
  workflow. Later tokenizer and Go-coverage fixes require distinct receipts;
  do not relabel older frozen results as current runs.
- [Serena's tools documentation](https://oraios.github.io/serena/01-about/035_tools.html)
  includes an optional v2 REPL that composes
  operations in one MCP call. Competitor configuration must capture the chosen
  interface; a classic one-tool/one-hop comparison is not a complete product
  comparison.
- Definition identity issue [#871](https://github.com/sysevol-ai/CodeNib/issues/871)
  remains a correctness concern to scope before admission. The ARB containment
  walk guard in #870 is not evidence that the underlying identity issue is fixed.

## G0-G1: reproduce and support daily use

The [2026-10-26 receipts](codegraph_workflow_2026_10_26.json) retain exact
synthetic fixture harnesses, commands, runtime/source identities and observed
MCP replies. Source baseline `5f0b7e9d` completes clean cold/warm setup in
1.563/0.637 seconds and rejects dirty CLI initialization. Its direct compiler
can rebuild the dirty tree, and a fresh MCP session returns the renamed
function. This is not a separately installed published-release control.

The candidate accepts dirty first use and tracked edits, creates, deletes and
renames; each 1.389-1.455 second initialization preserves visible file hashes
and Git status. Fresh stdio sessions return the updated call target and file.
The release smoke also verifies unchanged reuse, native registration reuse,
real graph/source MCP composition, Dependency Map serving and a dirty rebuild.
Client CLIs are isolated stand-ins; no coding agent/model or external user ran.
These small-fixture timings do not measure net task benefit.

Admission remains blocked by [#876](https://github.com/sysevol-ai/CodeNib/issues/876):
an existing MCP session returns `compute()` after the fixture source changes
to `compute_v2`, including after successful index rebuilding. The response has
no stale/source label even though `codegraph status` reports the old index's
source mismatch. A fresh session returns `compute_v2()`. Fix this serving and
lifecycle boundary before running a paid matched trial. Also retain #871's
definition-identity gate; dirty initialization alone does not pass G1.

Use temporary repositories and isolated client profiles, without model calls.
Record commands, installed/runtime versions, source identities, timings and
actual outcomes for clean cold setup, unchanged reuse, a dirty initial tree,
tracked modifications, file creation/deletion/rename and edit-then-requery.
Distinguish usable current facts, explicit unavailable/stale results and errors.

Fix the reproduced product blockers, with focused regressions and current
documentation. Preserve source authority and user changes. Keep persistence
inside existing manifest-bound artifacts and Wiki boundaries; this goal does
not authorize a generic database, backend registry or retained overlay system.
Do not solve a daily-workspace requirement by publishing stale facts as current.

## G2: admit a matched four-arm pilot

Use the existing evaluation machinery where it fits. Freeze a versioned run
manifest before reading task answers or starting inference; no new benchmark
framework is required by this goal. The manifest must specify:

- **Arms:** the same coding agent with native tools only, with Serena, with
  `colbymchenry/codegraph`, and with CodeNib CodeGraph. Pin product commits or
  packages, client and language-provider versions, interface modes and startup
  instructions. Separate the released CodeNib baseline from any candidate fix.
- **Tasks:** 10-20 prospectively selected tasks across at least three repositories,
  including cross-file relations, change-impact/repair work and simple lookup
  controls. Freeze acceptance criteria and cohort membership before execution.
  Exclude previously exposed demonstration/evaluation answers from unseen claims;
  keep reference answers and grading feedback outside model inputs.
- **Model:** one exact model/version, reasoning/tool-choice/output settings and
  matching per-task resource bounds. Check current provider constraints before
  a paid run. Capture effective configuration rather than requested flags alone.
- **Fair use:** keep native read/search/edit/shell tools available to every arm;
  do not force CodeNib use. Isolate checkouts, homes and MCP registrations, and
  prevent an arm from accessing another tool through the shell. Record actual
  tool use; an installed but unused tool is an outcome.
- **Workflow:** measure first use with arm-specific installation/indexing costs,
  return use and queries after identical source edits. Keep cold and warm results
  separate, and freeze any amortization schedule before observing outcomes.
- **Accounting:** retain every attempted task, timeout, setup failure, update
  failure and retry. Capture wall time, recorded model usage, input/output/cache
  tokens, tool calls, index/update cost and task acceptance. Missing fees stay
  unknown; SDK usage is not a reconciled invoice. Do not infer source-read credit
  from requested ranges, generated summaries or elided replies.

Select task time or model usage as the single primary endpoint before execution.
Compare it with the best cohort-level result among eligible alternatives that
meet the acceptance gate; do not select a weaker baseline afterward. The other
endpoint must not regress by more than 10%, and cold/update costs must remain
visible. Define timeout treatment and complete evidence requirements in the run
manifest. Publish paired task outcomes and uncertainty; this small pilot does
not demonstrate statistical non-inferiority on an unseen population.

Admission remains open: tasks, acceptance fixtures, exact configurations and a
hard authorized paid-run budget are not yet frozen. Work on offline reproduction
and scoped fixes can proceed while these are prepared. A live experiment must
not silently substitute the graph-free search pair or grep/Jev for CodeGraph.

## G3: five-user repeat-use pilot

Maintainers own recruitment and consent. No invitation has been sent and no
participant is claimed. Each of five participants attempts setup on their own
repository and has a seven-day observation window after first use.

Record setup attempts/successes, time to the first accepted source-linked query,
whether the agent actually called the tool, maintenance help needed and
voluntary return on later work. Target at least three of five returning without
a reminder to invoke CodeNib for the measured task. Keep all enrolled users in
the denominator and label missing observations. This is an adoption pilot, not
an estimate of market retention.

Use consented receipts/interviews; do not add a telemetry platform or collect
private source by default. Website views, setup-link clicks, operator demos and
stars do not count as successful own-repository use.

## G4: review and product action

- **Promote:** G1 correctness, the frozen G2 net-benefit gate and G3 repeat-use
  acceptance pass. Publish the supported scope, costs and reproducible evidence.
- **Narrow:** a prospectively declared task group benefits, while broader work
  does not. Limit the product promise and default entry to the supported scope.
- **No-go:** an admitted pilot fails the gates. Record the result and reduce
  investment in the unsupported default rather than adding features to evade it.
- **Insufficient evidence:** configuration, costs, correctness or user observations
  are incomplete. Keep the objective active with explicit gates; missing data is
  neither a performance failure nor proof of benefit.

The review must reconcile code, local verification, documentation, PRs/issues
and any release decision. External recruitment or paid execution is a real gate;
planning artifacts and one merged fix cannot complete this objective.
A recorded correctness or performance failure can justify no-go before external
recruitment. Record unrun phases explicitly without claiming repeat-use evidence.
Insufficient evidence alone does not complete the objective.
