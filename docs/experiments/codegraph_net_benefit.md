# CodeGraph real-workflow net-benefit pilot

Owner: CodeNib product and CodeGraph maintainers.
Status: dirty initialization, stale-query protection, scoped Python identity
recovery and source-current same-session reloading are merged. All twelve
original matched workflows execute and their repairs pass the selected native
grading. Source review retains four protocol-unassessable observations; the
frozen outcome is insufficient evidence and promotion is held. Separate
model-free peer controls now pass the original lookup on all three sources.
Separate common-runtime controls retain all four arms' actual installation,
source lookup and source-addition update costs, including Codegraph's pending
replies and bounded retry. A full corrected source rubric, prospective benefit
protocol, broader provider coverage, representative workflows beyond these
controls, automatic rebuilding and external trials remain open. The separate
corrected v2 cohort now has a complete pre-inference
[protocol and executable/runtime freeze](codegraph_matched_protocol_v2.md);
its execution and assessment remain pending.
Established 2026-10-26
(Pacific); target review 2026-11-09.
Current outcomes and open gates belong to [adoption A0](../adoption_roadmap.md).
Tracking issue: [#874](https://github.com/sysevol-ai/CodeNib/issues/874).

## Product question and decision

Can a developer complete source-grounded coding tasks more effectively with
CodeGraph after paying its setup, indexing and update costs? The intended
initial use case is cross-file execution-path and change-impact analysis.
This pilot produces a promote, narrow, no-go or insufficient-evidence decision.
It does not promise a positive result or establish population-wide efficacy.

The pilot product gate is at least 20% improvement on prospectively selected
complete task wall time against the
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

The initialization fix merged in [#877](https://github.com/sysevol-ai/CodeNib/pull/877)
accepts dirty first use and tracked edits, creates, deletes and
renames; each 1.389-1.455 second initialization preserves visible file hashes
and Git status. Fresh stdio sessions return the updated call target and file.
The release smoke also verifies unchanged reuse, native registration reuse,
real graph/source MCP composition, Dependency Map serving and a dirty rebuild.
Client CLIs are isolated stand-ins; no coding agent/model or external user ran.
These small-fixture timings do not measure net task benefit.

The [2026-10-27 source-query guard receipts](codegraph_currentness_2026_10_27.json)
address [#876](https://github.com/sysevol-ai/CodeNib/issues/876). The source
baseline silently returned `compute()` from an existing session after editing
and rebuilding `compute_v2`. The candidate instead returns an explicit MCP
error with rebuild/restart instructions; a fresh session returns `compute_v2`.
The same real fixture covers source-preserving create/delete/rename rebuilds.
All eleven indexed tools are checked before and after their provider runs;
mid-query edits discard the result. Ordinary provider failures/cancellation
leave unchanged source usable, and context close waits for active queries.
Queued exploration and cancellation remain responsive while a provider runs.
Portable snapshots and per-request grep/Jev source capture keep their existing
contracts. The release graph/MCP/Dependency Map smoke passes.

The stale-query guard is merged in
[#878](https://github.com/sysevol-ai/CodeNib/pull/878), closing #876.
The archived [#871 identity receipts](codegraph_identity_2026_10_27.json)
reproduce the original strict Python product failure: SCIP 0.6.6 assigns
the same symbol ID to an enclosing method and a same-named nested definition.
The graph loses their separate ranges and includes a containment self-edge.
The guard merged in [#879](https://github.com/sysevol-ai/CodeNib/pull/879)
refuses cyclic containment at common graph save/load boundaries
and advances graph builder identity so old receipts rebuild. In the actual
fixture, graph rebuilding fails explicitly, BM25 stays fresh, and MCP returns
graph-unavailable while BM25 source queries work. Reference recursion remains
valid. The persisted graph field layout is unchanged; the shared Python
artifact boundary applies to serial and native decoder outputs.

That intrinsic check is mitigation. The current
[source identity recovery](codegraph_python_identity.md) restores physical
definitions and compiler-observed reference bindings, including noncyclic
class/member collisions. Strict IPython and pytest product builds, complete
serial/native parity and exact MCP queries now pass. These developer controls
do not replace the original rejected fixture, failed pilot admission or frozen
model outcomes. Broader provider accuracy remains gated. Full rebuilding
remains an explicit cost. Local manifest sessions now reload
published indexes in the same connection after authenticating current source.
The [reload receipts](codegraph_reload_2026_10_27.json) retain one real SCIP/BM25
stdio process across edits, creates, renames and deletes, with eight expected
stale-query rejections and thirty successful calls. Every build preserves the
visible source hashes and Git status. Deterministic checks cover queued queries,
compiler contention, responsive session/status lookup, preserved delivery
history and retryable cleanup. Portable snapshots retain their selected source;
queries do not trigger indexing. Automatic source-driven rebuilding remains
unimplemented. This synthetic developer regression does not establish net task
benefit or change any frozen matched-pilot outcome.

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

Complete task wall time is the single primary endpoint, selected on 2026-10-27
before matched-task execution. Native subscription billing does not establish
comparable per-task cash charges. The [SDK route contract](codegraph_sdk_route.md)
keeps recorded model usage secondary and requires its per-client normalization
to be frozen in the run manifest.
Compare it with the best cohort-level result among eligible alternatives that
meet the acceptance gate; do not select a weaker baseline afterward. The other
endpoint must not regress by more than 10%, and cold/update costs must remain
visible. Define timeout treatment and complete evidence requirements in the run
manifest. Publish paired task outcomes and uncertainty; this small pilot does
not demonstrate statistical non-inferiority on an unseen population.

The user authorized a $500 API ceiling and prefers existing Codex/Claude
account SDKs. Both native SDK/MCP connectivity probes succeeded without
supplied API keys; their forced-tool fixtures are excluded from task/adoption
results. Tasks, acceptance, exact model/tool configurations, per-run limits and
accounting are frozen in the [execution policy](codegraph_matched_policy_v3.json).
The [executable/runtime binding and source checks](codegraph_execution_2026_10_27.json)
precede the first matched query. [All twelve workflows](codegraph_matched_run_v1.md)
now complete; the selected original native grader resolves all twelve captured
repairs. The [source assessment](codegraph_matched_assessment_v1.md) reviews all
48 explanations, retaining four ambiguous original items and material errors in
every alternative. The frozen policy therefore admits no eligible complete
comparison. A separate retrospective correction preserves the original wording
and cannot establish a prospective gain.
The separate [peer source-admission controls](codegraph_peer_source_admission.md)
retain unattended configuration and first lookup on all three pinned sources,
including their preparation/controller failures and bounded source omissions.
They use preinstalled warm runtimes and do not replace the original failed
setups. The [common-runtime controls](codegraph_common_runtime_admission.md)
separately bind every arm to the same project environment and retain actual
installation and source-addition update costs. They do not freeze the next
model protocol or establish a whole-task benefit.
Work on offline reproduction and scoped fixes can proceed. A live experiment must
not silently substitute the graph-free search pair or grep/Jev for CodeGraph.

## G3: five-user repeat-use pilot

Maintainers own recruitment and consent. The user confirms there is currently
no recruitment program. No invitation has been sent and no
participant is claimed. Each of five participants attempts setup on their own
repository and has a seven-day observation window after first use.

Recruitment hand-off is prepared below; it has not been sent. Maintainers must
confirm volunteers and consent before starting any observation window:

> We are inviting five developers to try CodeNib on their own repository for
> seven days. Use it only when it helps your normal work; no repeated-use quota
> is required. Participation is voluntary and you can stop at any time. With
> your consent, we will record setup time, whether your agent used the tool,
> whether its answer helped, errors/help needed and later voluntary reuse.
> Private source, credentials, prompts and identifiable repository paths are
> excluded by default. Agree any additional shared evidence separately.

For each consenting volunteer, keep an anonymous participant ID, consent/date,
language and approximate repository size, first-use date and observation end.
Retain unsuccessful setups and withdrawals with their stated reasons. Use the
same short check-in fields for each observed work session: task category,
actual tool call, accepted source-linked answer, help needed and later reuse
without a reminder to invoke CodeNib. Do not infer use from installation alone.

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
