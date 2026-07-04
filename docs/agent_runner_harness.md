<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent runner harness roadmap

Status: active roadmap
Last revised: 2026-07-03

## Goal

CodeMiner's agent harness should turn repository context into reliable agent
progress. The target is not to clone a general terminal agent. The target is a
CodeMiner-native runtime where retrieved, graph-derived, and read-confirmed
context is tracked, compressed, verified, and reused with enough structure that
agents can exploit it instead of repeatedly rediscovering it.

The current localization runner proves that this direction is useful:
`compact_after_read` can cut repeated context cost while preserving accuracy.
That is a proof of concept for context lifecycle management, not the final
harness architecture.

## Current Baseline

`AgentRunner` is a synchronous think/act/observe loop over model tool calls. It
has pragmatic localization features: default grep/read tools, index-backed
skills, first-turn tool forcing, last-turn schema salvage, token-budgeted
history eviction, and an eager compact mode after the first successful read.

This is good enough for controlled localization experiments, but it is not yet
a durable coding-agent runtime. The missing substrate is:

- persistent session traces with replayable message and tool parts;
- item-level context provenance and budget decisions;
- semantic compaction rather than oldest-message eviction;
- snapshot, patch, revert, and targeted verification around edits;
- permission and path boundaries for filesystem tools;
- diagnostics feedback from LSP, tests, and graph validators.

## Design Principles

1. Context is state, not text. Every candidate, read span, graph edge, and
   verification result should have provenance, score, freshness, and consumption
   status.
2. The model should see evidence, not dumps. Raw retrieval results are a staging
   area; the prompt should receive compact verified anchors plus enough tail
   context to continue.
3. Optional graph tools are not the main interface. Experiments show agents
   prefer grep/read when left alone. Structured context should be composed by
   the harness when it is the right move.
4. Verification must close the loop. A final answer should cite read-confirmed
   spans, and edited code should pass the smallest relevant local check before
   broader CI.
5. Runs must be auditable. A failed or surprising run should answer what context
   was offered, what was read, what was discarded, why compaction happened, and
   what evidence supported the final answer.

## Lessons From The Runtime Comparison

The useful gap versus mature agent runtimes is not one feature. It is the
runtime substrate:

- session event streams should persist reasoning, tool inputs, tool outputs,
  errors, usage, patches, and step boundaries;
- compaction should summarize old work, preserve a recent tail, and prune old
  tool output instead of only dropping messages;
- snapshot metadata should wrap tool steps and edits so diffs and reverts are
  first-class;
- permission evaluation should be explicit for external paths and risky
  commands;
- tool loops should detect repeated identical calls, surface diagnostics, and
  cleanly handle aborts and retries.

CodeMiner should borrow these runtime ideas while keeping the interface centered
on compiler/index context rather than terminal UX.

The first runtime guard now records tool-loop trace events and skips repeated
identical non-`bash` tool calls after a small threshold. This is intentionally
not a full planner, but it prevents a common naive failure mode while preserving
the existing grep/read workflow.

Concrete references from opencode's `packages/opencode/src` tree make the gap
less abstract:

- `session/compaction.ts` treats compaction as a session operation with overflow
  checks, recent-tail selection, and tool-output pruning. CodeMiner's M2 should
  keep this shape, but feed it read-confirmed spans and graph context rather
  than generic chat history.
- `session/run-state.ts` separates busy/idle/cancel state from the model loop.
  CodeMiner's M4 should use the same boundary so long localization or edit runs
  can be cancelled, resumed, and inspected without scraping logs.
- `session/todo.ts` persists structured plan state and emits update events.
  CodeMiner does not need a user-facing todo UI first, but the harness needs the
  same durable objective/checkpoint primitive for autonomous milestone work.
- `tool/tool.ts` wraps tool execution with typed argument validation,
  truncation, metadata, and permission hooks. CodeMiner's M5 should make grep,
  read, bash, patch, and verification tools pass through a comparable envelope.

The important difference is the center of gravity: opencode is a general agent
runtime with strong session and tool boundaries; CodeMiner should be a
context-specialized harness whose state machine starts from indexes, graph
edges, read-confirmed evidence, diagnostics, and tests.

## Milestone Ladder

Work in milestone order. A milestone can continue receiving refinements after
the next one starts, but it must meet its gate before downstream conclusions are
trusted.

| Milestone | Focus | Gate |
| --- | --- | --- |
| M0 Feedback Loop | Fixed Haiku probe, trace-aware aggregation, honest result hygiene | Small probe runs, reports trace signals, and full reports fail fast on missing inputs |
| M1 Runtime Observability | Run trace, context ledger, duplicate-call guard | Every synthesis cell can explain tool calls, reads, compactions, and loop skips |
| M2 Compaction V2 | Deterministic summary, protected read anchors, recent working tail, stale tool-output pruning | Compact keeps answer recall within 0.02 of grep on the Haiku probe while reducing tokens or turns |
| M3 Preload-Aware Routing | Verify top candidates, mark false positives, fallback when context is thin | Behavioral/traversal categories do not regress while symbol/file-hint savings remain |
| M3.4 Static LSP Facade | LSP-shaped definition/reference tools backed by the static graph index | Tool correctness is unit-tested and adoption is measured separately from graph quality |
| M3.5 Harness-Scheduled Graph/LSP | Runner injects graph/LSP context when trace shows symbol/reference intent or search fan-out | Traversal improves without relying on the model to discover low-level graph tools |
| M4 Durable Session Runtime | Persistent event store, replay, resumable runs, provider-neutral message parts | A failed run can be replayed from disk without relying on raw logs |
| M5 Edit Harness Envelope | Snapshot, patch metadata, permission boundaries, diagnostics, targeted tests | First gates are green: edit audit artifacts capture before/after diffs, dirty-start preimages, revert script, smallest verification command metadata, a concrete audited edit-command runner, and an `AgentRunner.run` audit wrapper with persisted agent result/tool envelope metadata |

Autonomy rule: after each milestone slice, run the Haiku feedback probe or a
smaller smoke, keep the change if it passes the milestone gate, and continue to
the next slice without waiting for manual approval. Escalate only when the probe
shows a regression that is not diagnosable from trace data.

## Autonomous Execution Model

The runner work should advance in thin, measurable slices:

1. Implement one harness behavior behind an existing config or conservative
   default.
2. Add trace or aggregate fields that prove the behavior actually happened.
3. Run the smallest smoke first, then the 27-cell Haiku probe when the smoke
   passes.
4. Promote the slice only if recall stays inside the milestone gate and tokens,
   turns, no-read rate, duplicate rate, or fallback rate improve.
5. Update this document with the exact result, then continue to the next slice.

This avoids waiting for full dataset sweeps while still preventing local prompt
experiments from becoming untracked folklore.

## Current Checkpoint

2026-07-02 checkpoint:

- M0 first slice is implemented: compact-result aggregation now rejects missing
  required sweeps, recognizes historical sweep aliases, and reports format
  failures plus cost fields.
- M1 first slice is implemented: synthesis cells now persist an agent trace,
  context ledger, compaction events, and duplicate non-`bash` tool-call skips.
- M2 first slice is implemented: eager compaction now emits a deterministic
  exploration summary, preserves read anchors plus a recent working tail, and
  reports stale tool-output pruning.
- M2.2 passed the default 27-cell Haiku probe: `preinj_eager_compact` improved
  pooled `rec@5` by 0.074 versus grep, reduced mean tokens by 46,969, reduced
  mean turns by 4.2, and had no behavioral/symbol-hint/traversal category
  recall regression.
- M3.1 is implemented: preload candidates now enter the context ledger as
  offered spans, reads mark overlapping candidates as verified, compaction
  summaries report preload offered/verified counts, and feedback reports expose
  `preload` plus `pre_vfy%`.
- M3.2 first slice is implemented: when preload candidates exist but no
  candidate span has been read-confirmed, the runner records a `fallback` event,
  injects a grep/read fallback nudge, and blocks eager compaction from declaring
  triage complete on an unverified preload anchor.
- Haiku one-query symbol-hint smoke passed the M3.1 trace gate:
  `preinj_eager_compact` kept `rec@5=1.000`, reduced tokens versus grep by
  18,933, recorded `pruned_tool_result_chars=4189`, and showed span-aware
  preload verification at 40% with `fb%=0%` because the candidates were
  sufficiently confirmed.
- M3.2b passed the default 27-cell Haiku probe: `preinj_eager_compact`
  improved pooled `rec@5` from 0.704 to 0.815, reduced mean tokens from 93,547
  to 35,240, reduced mean turns from 9.9 to 5.0, and had no category recall
  regression. Trace inspection showed all 18 preload cells had at least two
  read-verified preload spans, so `fb%=0%` was expected rather than a missing
  fallback signal; compact-mode `pre_vfy%` was 34%.
- M3.2c first slice is implemented: wrong-preload fallback is covered at the
  runner layer and the feedback aggregate layer. Synthetic unverified preload
  cells now report `fallback=1.0`, `preload_verified=0.0`, and `fb%=100%`,
  complementing the natural 27-cell probe where verified preload kept
  `fb%=0%`.
- M3.3 first slice is implemented: a graph fan-out arm can detect search
  pressure, recover graph seeds from span-only preload candidates, inject
  compact graph neighbors, and report `gexp%` plus expanded-node counts. The
  focused 9-cell traversal probe showed `preinj_graph_fanout` improving pooled
  `rec@5` from 0.444 to 0.667 versus grep while reducing mean tokens from
  112,472 to 84,952 and mean turns from 12.0 to 11.0; `gexp%=33%`. This is
  useful evidence that graph context helps, but the current implementation is a
  post-run reroute, so the next slice must move the trigger into the live loop.
- M3.4a is implemented: `lsp_definition` and `lsp_references` expose
  LSP-shaped read-only navigation backed by the static symbol graph. Unit tests
  cover definition jumps from anchored references, symbol-seeded definitions,
  and reference-site anchors. A dedicated 9-cell traversal probe showed the
  free-tool `lsp_graph` arm had `lsp%=0%`, regressed pooled `rec@5` by 0.111
  versus grep, and increased tokens. Conclusion: the facade is useful substrate,
  but should not be promoted as a model-selected arm; the harness must schedule
  it when the trace shows a definition/reference or fan-out need.
- M3.3b/M3.4b implementation slice is in place: `AgentRunner` accepts a
  conservative live-loop context scheduler, the synthesis harness can inject
  bounded graph/LSP context once search fan-out, read fan-out, or turn-budget
  pressure appears, and feedback aggregation reports `sched%` plus
  scheduled-node counts. Two 4-cell Python/traversal smokes proved the live
  path and the failure mode: with `read_calls>=6` and 10 scheduled nodes,
  `preinj_graph_scheduled` triggered at turn 4 (`sched%=100%`, 10 nodes) and
  reached `rec@5=1.000` in 10 turns, but still cost more than the 8-turn compact
  run; with delayed 5-node injection it triggered at turn 5 and regressed to
  `rec@5=0.667` / 12 turns. Conclusion: the mechanism is real, but promotion
  needs gating. The current config skips scheduled graph context when at least
  two preload candidates are already read-verified, and feedback aggregation
  reports `sskip%` so a non-trigger can be distinguished from a deliberate skip.
- M3.3c guarded traversal probe ran on Python/Go/Rust with grep, compact,
  post-run graph fan-out, and live scheduled graph/LSP arms. The guarded
  scheduled arm improved over grep (`rec@5` 0.667 versus 0.556, mean tokens
  59,782 versus 135,141, mean turns 7.0 versus 12.0), but it did not beat eager
  compact or post-run graph fan-out (`rec@5` 0.778 for both). The guard did its
  job: `sched%=0%`, `sskip%=67%`, and the two skips were explained by
  read-verified preload anchors. Conclusion: the live mechanism is integrated
  and measurable, but it should not be promoted as a default traversal strategy
  until the scheduler can distinguish thin or contradictory context from
  already-confirmed preload context.
- M3.4c is implemented: the runner now passes successful read outputs into the
  live context scheduler, the scheduler extracts narrow Rust-style
  `Type::Variant` definition intent from read windows, and the static graph/LSP
  route can inject exact definition anchors for those symbols. The symbol
  extractor deliberately promotes a base type such as `Expr` when one read
  window mentions several `Expr::*` variants, so generated AST enum definitions
  are not crowded out by leaf wrapper nodes.
- M3.4c passed the targeted Rust traversal probe twice after the base-type
  ranking fix. In the final single-instance run, `preinj_graph_scheduled`
  reached `rec@5=1.000` in 4 turns and 29,561 tokens, while
  `preinj_eager_compact` and `preinj_graph_fanout` stayed at `rec@5=0.333`.
  The trace shows a real static-LSP path, not a format accident:
  `read_symbols` scheduled `Expr`, `ExprTuple`, `ExprList`, `ExprSet`, and
  `ExprCall`, injected `crates/ruff_python_ast/src/nodes.rs:565-634`, and the
  final answer cited that span.
- M3.4c also passed the 12-cell Python/Go/Rust traversal feedback probe:
  `preinj_graph_scheduled` had pooled `rec@5=0.889`, `files@5=1.000`,
  mean tokens 56,811, and mean turns 7.0. It beat grep (`rec@5=0.556`,
  135,140 tokens, 12.0 turns), eager compact (`rec@5=0.667`, 38,999 tokens,
  6.3 turns), and post-run graph fan-out (`rec@5=0.778`, 71,229 tokens,
  9.3 turns). Residual risk: the Go scheduled cell deliberately skipped graph
  injection because preload was already read-verified and scored `rec@5=0.667`
  in a sample where grep happened to hit `rec@5=1.000`; this keeps the
  scheduler in feedback-probe promotion rather than unconditional default
  promotion.
- M3.4d first slice is implemented: scheduled graph/LSP answer anchors now have
  a deterministic audit path. When `graph_schedule_audit_answer` is enabled,
  the harness inspects scheduled `read_symbols` definition anchors, confirms
  which ones were actually read, parses the committed answer's `Locations`, and
  permits one bounded correction run only when no read-confirmed scheduled
  anchor was cited. Feedback reports expose this as `aud%`, with offered/read/
  cited counts in the JSON aggregate. Unit coverage protects the three critical
  cases: read-but-uncited anchor triggers, cited anchor does not trigger, and
  un-read scheduled hints do not trigger.
- M3.4d passed the targeted Rust traversal audit probe. The scheduled arm kept
  `rec@5=1.000` in 4 turns and 30,667 tokens, versus `rec@5=0.333` for compact
  and graph fan-out. The audit did real work but did not add cost:
  `aud%=0%`, with scheduled anchor counts `offered=5`, `read=5`, `cited=5`.
  This validates the happy path where the model already cites read-confirmed
  static-LSP anchors; the remaining M3.4e gate is the full traversal feedback
  probe to make sure correction remains low-frequency across languages.
- M3.4e passed the 12-cell Python/Go/Rust traversal audit gate. With
  `graph_schedule_audit_answer` enabled, `preinj_graph_scheduled` reached
  pooled `rec@5=0.889`, `files@5=1.000`, mean tokens 70,523, and mean turns
  8.0. It beat grep (`rec@5=0.444`, 130,312 tokens, 12.0 turns), eager compact
  (`rec@5=0.667`, 59,455 tokens, 8.0 turns), and post-run graph fan-out
  (`rec@5=0.667`, 43,885 tokens, 6.0 turns). The scheduled-anchor audit stayed
  cold (`aud%=0%`): Rust offered/read/cited all 5 static-LSP anchors, while the
  Python and Go scheduled cells skipped injection because preload was already
  verified or turn-budget pressure arrived after the skip gate. Residual risk:
  scheduled trigger coverage is still only 33% on this probe, so the next
  promotion target is broader static-LSP routing rather than changing the audit
  correction loop.
- M3.4f broadened the static-LSP scheduler beyond Rust scoped variants. The
  first naive attempt proved the failure mode: generic Python/Go symbol
  extraction raised `sched%=100%` but regressed the scheduled arm to pooled
  `rec@5=0.667`, with early Python anchors such as `auto_open`, `methods`, and
  `_auto_open_files` crowding out better definition jumps. The promoted slice
  now extracts generic symbols only from call-site-like read lines, ignores
  declaration lines, keeps a higher default read threshold for generic symbols
  than for scoped Rust symbols, and unit-tests both the trigger and wait paths.
  On the final 12-cell Python/Go/Rust traversal gate,
  `preinj_graph_scheduled` reached pooled `rec@5=0.889`, `files@5=1.000`,
  mean tokens 74,119, and mean turns 8.3. It beat grep (`rec@5=0.333`,
  136,472 tokens, 12.0 turns), eager compact (`rec@5=0.667`, 65,523 tokens,
  8.0 turns), and post-run graph fan-out (`rec@5=0.667`, 83,823 tokens,
  10.7 turns). `sched%=100%`, `aud%=0%`, and `sskip%=0%`; Python and Go both
  reached `rec@5=1.000` through scheduled `read_symbols`, while Rust reached
  `rec@5=0.667`. Residual risk: Python still included same-file fields/methods
  such as `_auto_open_files`, `_today`, and `_expires` before useful cross-file
  anchors, so the next slice must add query/trace-aware ranking instead of
  merely increasing trigger coverage.
- M3.4g added query-aware symbol ranking and same-file noise suppression. The
  first 12-cell Python/Go/Rust traversal run reduced Python same-file noise and
  kept the Rust static-LSP path intact, but Go still exposed the budget problem:
  `SplitHostPort`, `WithValue`, `ReplacerCtxKey`, and even `func` could crowd
  out the replacement bridge method before the five-node definition limit. The
  follow-up query-aware stopword and key-penalty slice removed `func` and
  demoted context-key symbols, but a Go-only gate still showed the scheduler
  injecting `NewReplacer` without `Replacer#ReplaceAll` or `ReplaceOrErr`.
  Conclusion: query matching is necessary but not sufficient; the scheduler
  also needs semantic role/budget pressure for bridge methods.
- M3.4h added a conservative replacement-bridge boost for placeholder, token,
  template, variable, and resolution queries. This is not Caddy-specific: it
  promotes `Replace*` and `NewReplacer`-style bridge symbols only when the
  query itself asks about template or replacement flow, while continuing to
  demote context-key anchors. The Go-only traversal gate closed the previous
  failure: scheduled static-LSP context injected `NewReplacer`,
  `Replacer#ReplaceAll`, `Replacer#ReplaceKnown`, `ParseNetworkAddress`, and
  `HealthChecks` at turn 3. The scheduled arm matched grep's `rec@5=0.667` and
  `files@5=1.000` while reducing cost from 132,916 to 60,725 tokens and from
  12 to 7 turns. This validates the bridge-node budget path on the known Go
  failure case; promotion still requires a fresh Python/Go/Rust traversal gate.
- M3.4i fixed the audit-correction reuse bug. The scheduled context scheduler
  is now recreated when a bounded audit correction run is launched, so the
  correction pass can inject fresh static-LSP anchors instead of inheriting the
  already-consumed scheduler state from the first attempt. This was required
  before judging audit behavior: otherwise a failed final answer could receive
  only a textual correction instruction, with no renewed graph/LSP evidence.
- M3.4j tightened the scheduled static-LSP prompt and added a no-GT answer
  ordering audit. Scheduled definitions are now described as candidate answer
  anchors; the prompt asks the first five `Locations` entries to prefer route
  endpoints, bridge/factory definitions, and provider/value definitions, and to
  keep exact helper ranges out of explanatory prose unless they belong in the
  final shortlist. The Go-only traversal gate validated the slice:
  scheduled reached `rec@5=1.000` in 10 turns and 84,502 tokens, after the
  prior failure mode where helper methods crowded out `globalDefaultReplacements`
  or the health-check caller.
- M3.4k added a priority top-anchor audit and exposed the new correction
  signals in feedback reports as `taud%`/`taud_m` plus ordering-audit
  `ord%`/`ord_n`. The audit checks whether the first read-confirmed scheduled
  anchor appears in the answer's top five locations, then permits one bounded
  correction run with a fresh scheduler only when that priority anchor was
  pushed below the cutoff. On the 12-cell Python/Go/Rust traversal gate,
  `preinj_graph_scheduled` reached pooled `rec@5=1.000`, `files@5=1.000`,
  mean tokens 105,214, and mean turns 11.3. It beat grep (`rec@5=0.444`,
  135,133 tokens, 12.0 turns), eager compact (`rec@5=0.778`, 76,872 tokens,
  9.0 turns), and graph fan-out (`rec@5=0.667`, 48,969 tokens, 6.7 turns).
  `sched%=100%`, `aud%=0%`, `taud%=33%`, and `ord%=0%`: Python needed one
  top-anchor correction to lift `_get_download_cache_loc` into the first five
  locations, while Go and Rust reached `rec@5=1.000` without post-answer
  ordering correction. Residual risk: accuracy is now strong, but scheduled is
  still costlier than compact/fanout. The next slice should move the successful
  correction signal into scheduler-time role quotas instead of relying on
  post-run re-answering.
- M3.4l/M3.4m moved the top-anchor correction signal into scheduler-time role
  quotas for endpoint, bridge/factory, provider/value, and type/base-variant
  anchors. The local selector is unit-tested for the known failure modes:
  cache-route provider promotion, provider-over-support ordering, type-family
  preservation, and Go replacement bridge retention. The Python-only traversal
  validation was a strong positive: scheduled reached `rec@5=1.000` in
  40,331 tokens and 6 turns, down from the 57,464-token / 8-turn compact arm.
  The full Python/Go/Rust traversal gate did not promote the slice:
  scheduled reached `rec@5=0.778`, `files@5=1.000`, 111,304 mean tokens, and
  12.7 mean turns. That improved recall over grep but did not beat post-run
  graph fan-out on recall or cost, and was much costlier than compact. The
  trace explains why: Go and Rust got better candidates, but the model still
  had to infer the route from point definitions. Conclusion: role sorting is a
  useful primitive, but the harness needs a first-class static-LSP route
  contract rather than more ad hoc definition ranking.
- M3.5a first slice is implemented: `lsp_route` exposes a graph-backed,
  LSP-shaped semantic route map over the static symbol index. It resolves
  multiple symbol seeds, role-scores direct definitions as endpoint,
  bridge/factory, provider/value, type, or support anchors, adds
  query-relevant one-hop `via` neighbors, and keeps type-heavy AST families
  from being polluted by weak neighbors. The runner prompt, design-space
  `lsp_graph` arm, Haiku LSP facade probe, fan-out accounting, and feedback
  `lsp%` metric now include `lsp_route`. Focused verification passed:
  graph navigation, prompt, tool schema, feedback aggregate, harness graph
  fan-out, and verify-expand tests all pass in the 87-test local slice.
- M3.5b implementation slice is in place: the live read-symbol scheduler now
  prefers a `route_for_symbols()` graph-nav protocol and records
  `operation: lsp_route`, route roles, and role/via-tagged locations in the
  scheduled context metadata. The old definition and role-quota paths remain as
  fallbacks for nav implementations that do not expose route semantics. Focused
  verification passed in the 112-test runner/harness slice.
- M3.5b gate passed on the 3-cell Haiku Python/Go/Rust traversal probe:
  `preinj_graph_scheduled` reached pooled `rec@5=0.778`, `files@5=0.833`,
  mean tokens 81,816, and mean turns 10.0. It beat grep (`rec@5=0.333`,
  136,486 tokens, 12.0 turns), eager compact (`rec@5=0.556`, 93,243 tokens,
  11.0 turns), and graph fan-out (`rec@5=0.444`, 139,807 tokens, 18.0 turns).
  The scheduled trace events show `operation: lsp_route` for all three cells,
  with `sched%=100%`, `sched_n=8.0`, `taud%=33%`, and `ord%=0%`. Python and
  Go reached `answer_rec@5=1.000`; Rust improved over grep/fanout but stayed
  at `answer_rec@5=0.333` on a type-heavy route. Residual risk: Go still needed
  a priority top-anchor correction, costing 15 turns and 119,791 tokens, and
  Rust shows that type-only route context is not yet enough answer guidance.
  The next slice should reduce correction cost and type-route misses before
  adding broad new LSP facets. The probe also caught a top-level scheduled-state
  merge bug where post-answer audit/correction could overwrite
  `scheduled_context_operation`; the correction-state merge is fixed, while the
  historical gate artifact proves route usage through trace events.
- M3.5c is implemented: feedback reports now promote scheduled operation usage
  from trace trivia to first-class probe fields. The report table exposes
  `route%`, `sdef%`, `sfan%`, and `op?%` next to `sched%`, so future gates can
  distinguish graph/LSP route context from fallback definition or fan-out
  context without manual JSON inspection. Re-aggregating the M3.5b gate showed
  `preinj_graph_scheduled route%=100%`, confirming all three scheduled cells
  used the static-LSP route operation. The JSON writer now also backfills
  missing top-level `scheduled_context_operation` fields from the persisted
  scheduled-context trace event.
- M3.5d promoted a narrower route-ranking/audit slice. Type-heavy routes now
  reserve one slot for a high-query-overlap direct endpoint/bridge/provider
  instead of spending every slot on AST type definitions, and the top-anchor
  audit prefers richer route anchors over one-line declarations. On the same
  3-cell Haiku Python/Go/Rust traversal gate, `preinj_graph_scheduled` held
  pooled `rec@5=0.778` and `files@5=0.833` while reducing mean tokens from
  81,816 to 68,515 and mean turns from 10.0 to 8.0. `route%=100%`, `sched%=100%`,
  `taud%=0%`, and `ord%=0%`. Go stayed at `answer_rec@5=1.000` and dropped from
  15 turns / 119,791 tokens to 11 turns / 101,565 tokens, so the top-anchor
  correction loop is no longer needed on this sample. Rust still stayed at
  `answer_rec@5=0.333`, but the scheduled route now includes a non-type endpoint
  and the cell fell from 74,606 tokens / 8 turns to 50,669 tokens / 6 turns.
  The next slice should focus on answer guidance or facet choice for type-heavy
  Rust routes, not on more Go correction machinery.
- M3.5e first slice is positive on the Rust failure cluster: type-heavy route
  prompts now state that AST type/base-variant definitions are final-answer
  evidence when the query asks how tuple/list/set/name/call expressions are
  recognized. A Rust-only Haiku traversal probe improved
  `preinj_graph_scheduled` from the prior Rust `answer_rec@5=0.333` to
  `answer_rec@5=0.667`; the final Locations line included
  `crates/ruff_python_ast/src/nodes.rs:565-634` instead of citing only rule
  helpers. This is not a full promotion yet: the scheduled Rust cell used
  104,928 tokens and 12 turns, up from M3.5d's 50,669 tokens and 6 turns. The
  next slice should preserve the type-anchor answer gain while reducing the
  extra reads/turns, then rerun the full Python/Go/Rust traversal gate. The
  sweep writers also now persist top-level `scheduled_context_operation` from
  `run_cell`, matching the trace-backed `route%` report.
- M3.5f-i narrowed the Rust `lsp_route` failure into three concrete harness
  issues and fixed the first two. First, route symbols were too dependent on the
  already-read branch; a wrong first read of Ruff's
  `unnecessary_iterable_allocation_for_first_element` rule caused the scheduler
  to amplify that branch instead of preserving the retrieval-preloaded
  `perflint/unnecessary_list_cast` endpoint. The scheduler now merges bounded
  preload-derived symbol hints, ranks fallback symbols by query-leaf overlap,
  filters bracket-heavy impl helpers, and query-completes `ExprTuple`,
  `ExprList`, and `ExprSet` when an `Expr` family read appears in an ordered
  collection query. Second, answer scoring undercounted correct Markdown bullet
  `Locations:` continuations because the markdown-label regex used `\s` and
  crossed line boundaries; the parser now uses horizontal whitespace and parses
  explicit bullet/numbered continuation lines. The runner, route prompt, and
  scheduled audit corrections also ask for plain single-line, comma-separated
  `Files:/Symbols:/Locations:` contracts.
- The M3.5i scheduled-only Rust gate with these fixes reached
  `answer_rec@5=1.000`, `files@5=1.000`, 103,150 tokens, and 10 turns. Its
  first five answer spans included `unnecessary_list_cast.rs:53-124`,
  `nodes.rs:565-634`, and the combined `ExprList`/`ExprTuple` span
  `nodes.rs:2824-2870`. This is a correctness promotion for the Rust failure
  cluster but not a cost promotion: it still reads 10 spans and sometimes chases
  semantic helpers. Current route-selector unit tests now pin the desired
  ordering: `unnecessary_list_cast` must beat
  `unnecessary_literal_within_deque_call` for this query family. The next slice
  should thin type-heavy route context and reduce follow-up reads while keeping
  this `rec@5=1.000` Rust behavior.
- M3.5j completed the Rust cost-promotion slice and exposed an important route
  hygiene bug. The selector now thins type-heavy `lsp_route` context to the
  base `Expr`, at most two query-implied variants, and one high-signal route
  endpoint; rank-2 variants such as `ExprCall`/`ExprName` no longer fill spare
  slots just because the budget is available. A first rerun still failed
  because the scheduler passed `effective_query` into route ranking after eager
  preload had appended code snippets; those snippets polluted query terms with
  `call`, `name`, and unrelated rule names, reintroducing wrong AST variants
  and the `unnecessary_literal_within_deque_call` endpoint. The promoted fix
  makes the live context scheduler route with the original clean user query and
  records `query_chars` in scheduled-context trace metadata.
- The final M3.5j Rust scheduled-only gate reached `answer_rec@5=1.000`,
  `files@5=1.000`, 28,364 tokens, and 4 turns. The route trace used
  `operation=lsp_route`, `route%=100%`, `sched_n=4`, and the clean-query
  anchors were exactly `Expr`, `ExprTuple`, `ExprList`, and
  `unnecessary_list_cast`; the final answer cited
  `unnecessary_list_cast.rs:52-124`, `nodes.rs:565-634`,
  `nodes.rs:2824-2830`, and `nodes.rs:2861-2870`. This promotes the Rust
  failure cluster on both correctness and cost. Residual risk: the full
  Python/Go/Rust traversal gate still needs to confirm that clean-query route
  ranking preserves the prior no-correction behavior outside Rust.
- M3.5k/M3.5l passed the full Python/Go/Rust traversal gate after two route
  hygiene fixes. The first clean-query full gate held Rust at `rec@5=1.000`
  but only reached pooled `rec@5=0.778`: Python omitted the cache-check span
  from the top five answer locations, and Go missed the health-check loop
  caller. Trace inspection showed route clutter, not missing graph data:
  single-line non-callable symbols such as `_auto_open_files` and
  `HealthChecks` were being promoted as endpoints, while direct bridge/endpoint
  anchors were being pushed behind route-neighbor noise. The promoted slice
  demotes single-line non-callable direct symbols to support, filters generic
  read hints such as `file`, `files`, `download`, and `seconds`, boosts
  query-overlapping method endpoints, and sorts remaining route budget slots so
  direct anchors beat generic route neighbors except for high-signal provider
  neighbors such as `_get*` and `globalDefault*`.
- The final M3.5l three-cell gate reached pooled `answer_rec@5=1.000`,
  `files@5=1.000`, 34,758 mean tokens, and 5.7 mean turns, with
  `sched%=100%`, `route%=100%`, and no answer/top-anchor/order corrections.
  Per-cell results: Python `rec@5=1.000` in 8 turns / 61,600 tokens, Go
  `rec@5=1.000` in 5 turns / 24,071 tokens, and Rust `rec@5=1.000` in
  4 turns / 18,602 tokens. The Go route now starts with
  `doActiveHealthCheckForAllHosts`, `NewReplacer`, and
  `globalDefaultReplacements`; Rust keeps the thin four-node type route.
  Residual risk: the Python trace still finds `is_url_in_cache` through
  follow-up grep/read rather than as an explicit scheduled anchor, so the next
  cost slice should make cache-check provider anchors first-class instead of
  relying on the model to rediscover them.
- M3.5m is implemented and passed the final Python/Go/Rust scheduled-only
  traversal gate. The slice made route context more harness-driven rather than
  only better sorted: cache/check/provider anchors such as `is_url_in_cache`
  are promoted into the scheduled route, one-line hash-named fields are demoted
  out of endpoint priority, the scheduler auto-opens the highest-priority
  route source, type-heavy routes auto-open base plus first variant anchors,
  and a type-heavy finalization guard stops further broad search after the
  route evidence is available. The final gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35m_full_guard` reached
  pooled `rec@5=1.000`, `files@5=1.000`, 35,931 mean tokens, and 5.3 mean
  turns, with `sched%=100%`, `route%=100%`, `taud%=0%`, and `ord%=0%`.
  Per-cell results: Python `rec@5=1.000` in 8 turns / 61,686 tokens, Go
  `rec@5=1.000` in 4 turns / 13,191 tokens, and Rust `rec@5=1.000` in
  4 turns / 32,917 tokens. The Python scheduled route now explicitly includes
  `is_url_in_cache` and marks `LeapSeconds.auto_open()` as `via is_url_in_cache`;
  the Go route auto-opens `doActiveHealthCheckForAllHosts` and no longer needs
  a top-anchor correction; the Rust route uses a finalization guard over
  `unnecessary_list_cast`, `Expr`, and `ExprTuple` instead of exhausting the
  turn budget on semantic helper exploration. Residual risk: this is still a
  three-query feedback gate, and Python cost remains variable, so the next
  slice should scale the scheduled-route validation set before adding new LSP
  facets.
- M3.5n is implemented and passed the expanded scheduled `lsp_route` traversal
  q2 gate. The first 6-cell run at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35n_q2` exposed two real
  failures: Go trace-back answered after three generic reads without scheduled
  route context (`rec@5=0.5`), and Rust trace-back found route context but
  missed `add_iter` while spending 24 turns / 415k tokens. The fix makes
  trace-back style queries trigger generic read-symbol routing after three
  reads, expands traceback route budgets to 12 route anchors / 30 pool nodes /
  80 neighbour nodes, extracts Rust SCIP impl method leafs such as
  `run_action_queue`, suppresses lowercase `line` symbol noise, auto-opens two
  traceback route anchors by default, and lets finalization guards promote
  read-confirmed route anchors even when they were outside the initial top five
  route list. Focused probes passed:
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35n_go_fix` reached
  `rec@5=1.000`, `files@5=1.000`, 27,337 mean tokens, and 5.0 mean turns;
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35n_rust_guard_fix`
  reached `rec@5=1.000`, `files@5=1.000`, 30,107 mean tokens, and 4.0 mean
  turns. The final 6-cell gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35n_q2_fix` reached pooled
  `rec@5=1.000`, `files@5=1.000`, 30,649 mean tokens, and 5.0 mean turns, with
  `sched%=83%`, `route%=83%`, `taud%=17%`, and `ord%=0%`. Residual risk: the
  full q2 run still showed Go trace-back cost variance (10 turns in the pooled
  run versus 5 turns in the focused Go run), so the next slice should stabilize
  traceback route finalization before adding more static-LSP facets.
- M3.5o is implemented and passed the scheduled `lsp_route` q2 stability gate.
  The first M3.5o attempts exposed two audit/guard failure modes:
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35o_q2_final` regressed
  Rust trace-back to `rec@5=0.5` because `add_iter` was still outside the
  protected final guard, and
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35o_rust_full_guard_audit`
  showed that protecting the whole guard can force a costly correction without
  fixing guard order. The fix makes traceback guard ranking query-aware,
  auto-opens traceback anchors in guard order, stores a bounded
  `priority_count` on finalization guards, and suppresses top-anchor correction
  for provider-only scheduled routes that do not have endpoint/type priority
  anchors. Focused probes passed:
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35o_rust_priority_count`
  reached `rec@5=1.000`, `files@5=1.000`, 32,308 mean tokens, 4.0 mean turns,
  and `taud%=0%`, while
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35o_python_provider_filter`
  reached `rec@5=1.000`, `files@5=1.000`, 31,478 mean tokens, 4.0 mean turns,
  and `taud%=0%`. The final 6-cell gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35o_q2_provider_filter`
  reached pooled `rec@5=1.000`, `files@5=1.000`, 30,587 mean tokens, and 4.8
  mean turns, with `sched%=100%`, `route%=100%`, `taud%=0%`, and `ord%=0%`.
  Residual risk: Python trace-back still shows exploration variance (7 turns /
  55,627 tokens in the final q2 gate), so the next slice should use trace rows
  to reduce Python traversal reads without weakening the now-stable Rust/Go
  traceback guard path.
- M3.5p is implemented and passed the scheduled `lsp_route` q2 variance gate.
  The promoted slice skips provider/support-only scheduled routes before they
  enter the live prompt: if `route_for_symbols()` returns no endpoint, bridge,
  or type anchor, the scheduler records `skip_reason=weak_route_roles` and
  leaves the agent on the existing read-confirmed path. A focused Python probe
  at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35p_python_weak_route_skip`
  showed the intended failure-mode fix: Python trace-back dropped from the
  M3.5o q2 outlier of 55,627 tokens / 7 turns to 31,555 tokens / 6 turns while
  keeping `rec@5=1.000` and recording `sskip%=50%`. The final 6-cell q2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35p_q2_weak_route_skip`
  reached pooled `rec@5=1.000`, `files@5=1.000`, 22,373 mean tokens, and 4.0
  mean turns, with `sched%=83%`, `route%=83%`, `fg%=67%`, `taud%=0%`, and
  `ord%=0%`. This is a cost and stability promotion over M3.5o's 30,587 mean
  tokens / 4.8 turns.
- A follow-up no-final-guard A/B is explicitly rejected. Disabling
  `graph_read_symbol_final_guard` on the same q2 surface at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35q_q2_no_final_guard`
  regressed the scheduled arm to pooled `rec@5=0.917`, 109,942 mean tokens,
  and 10.8 mean turns. The trace pattern was consistent: without the
  finalization guard, Go and Rust trace-back/bridge cells continued into
  grep/read/bash exploration even after strong route anchors had been offered.
  Feedback reports now expose finalization-guard usage as `fg%`/`fg_n`, so
  future static-LSP route iterations can distinguish the three route lifecycle
  stages: scheduled route context, auto-opened evidence, and final guard.
  Residual risk: final guards are now proven useful on q2, but they remain a
  blunt lifecycle boundary. The next slice should make their timing and
  protected-anchor set more trace-aware rather than removing them or adding
  unrelated LSP facets.
- M3.5q is implemented and passed the q2 route-lifecycle timing gate. The
  scheduler now defaults `graph_read_symbol_final_guard_delay_turns` to 1
  instead of 2, so a route/type/traceback finalization guard can fire after one
  post-route model turn rather than waiting for two more turns of search. The
  q2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35q_q2_guard_delay1`
  reached pooled `rec@5=1.000`, `files@5=1.000`, 17,178 mean tokens, and 4.0
  mean turns, with `sched%=83%`, `route%=83%`, `fg%=67%`, `sskip%=17%`,
  `taud%=0%`, and `ord%=0%`. This improves on M3.5p's 22,373 mean tokens while
  keeping the same accuracy and turn count. The feedback aggregate also fixed a
  reporting bug: skipped weak routes no longer count as `route%`, so
  `route%` means scheduled route context was actually offered, while `sskip%`
  explains attempted-but-suppressed routes. Residual risk: Python trace-back
  remains stochastic after a weak-route skip (49,193 tokens / 7 turns in this
  q2 run), so the next slice should improve the post-skip convergence path
  rather than changing the proven route/guard path.
- M3.5r is implemented and passed the q2 weak-route aftermath gate. When
  `route_for_symbols()` returns only weak provider/support anchors, the
  scheduler still suppresses those anchors, but now injects a small
  `lsp_route_weak_skip` convergence nudge: do not broaden search because of
  the weak static route result; if the already-read spans explain the request,
  finalize from those exact read-confirmed locations. A focused Python probe at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35r_python_weak_nudge`
  confirmed the target behavior: Python trace-back used
  `lsp_route_weak_skip` at turn 3 and reached `rec@5=1.000` in 4 turns /
  12,519 tokens, down from the M3.5q weak-skip outlier of 49,193 tokens /
  7 turns. The final q2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35r_q2_weak_nudge` reached
  pooled `rec@5=1.000`, `files@5=1.000`, 15,517 mean tokens, and 3.5 mean
  turns, with `sched%=100%`, `route%=83%`, `fg%=67%`, `sskip%=17%`,
  `taud%=0%`, and `ord%=0%`. Residual risk: this is still a six-cell q2 gate;
  Python bridge token variance remains visible even though pooled cost improved.
  The next slice should broaden the scheduled-route sample or add repeat
  variance checks before new LSP facets.
- M3.5s is implemented: the feedback aggregate now folds repeated cells by
  query/arm while surfacing repeat variance instead of hiding it behind the
  min-of-reps headline. Reports include `rep_n`, `rec_min`, `tok_hi`,
  `tok_rng`, `turn_hi`, and `turn_rng`. The Python scheduled-route repeat gate
  at `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35s_python_repeat3`
  kept `rec_min=1.000` across two traversal queries with three reps each, but
  showed the real cost issue: min-of-reps mean tokens were 11,234 while
  worst-run mean tokens were 27,942 and mean token range was 16,708. Bridge
  route context injected 9,362 chars each time, including source that had
  already been read.
- M3.5t was deliberately not promoted. A naive traceback route node budget cut
  the bridge route from 12 anchors to 8 and reduced injected context, but it
  dropped `is_url_in_cache()` from the route, causing bridge `rec_min` to fall
  to 0.667 in
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35t_python_route_budget_repeat3`.
  This is the important LSP-route lesson: node-count pruning is unsafe unless
  the budgeter preserves query-overlapping provider anchors.
- M3.5u is implemented and passed the Python repeat gate. The scheduler now
  skips auto-opening route spans that overlap successful `read` calls, and the
  route node budget remains available only as an explicit opt-in config rather
  than a default. The repeat gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35u_auto_open_dedupe_repeat3`
  kept `rec_min=1.000` and `files@5=1.000` across all six cells. Bridge
  scheduled-context size fell from 9,362 to 5,751 chars. The stability metrics
  improved on the worst runs (`tok_hi` 27,942 -> 26,148; `tok_rng` 16,708 ->
  12,095), although the min-of-reps token headline rose from 11,234 to 14,052.
  Conclusion: auto-open de-duplication is safe and useful for worst-case
  variance, but route-budget pruning needs a provider-retention design before
  it can be enabled by default.
- M3.5v implemented the provider-retaining route budget primitive, but did not
  promote it as a default. The budgeter now protects direct definitions that
  are the `via` symbol for a retained route-neighbor anchor, so a budgeted
  cache route keeps `is_url_in_cache()` instead of replacing it with weaker
  support/provider nodes. Unit coverage pins that behavior. The explicit
  budget=8 repeat gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35v_provider_budget_repeat3`
  restored the M3.5t recall loss (`rec_min=1.000`, `files@5=1.000`) and shrank
  bridge scheduled-context size further to 5,245 chars. The cost signal was
  mixed: `tok_rng` improved to 4,434, but min-of-reps tokens rose to 22,562 and
  `tok_hi` was 26,996, slightly worse than M3.5u's 26,148. Conclusion: the
  provider-retention rule is correct harness substrate, but budget=8 should
  remain opt-in until a broader gate shows net cost improvement.
- M3.5w implemented conservative dynamic route-budget gating and trace
  visibility. When `graph_read_symbol_dynamic_route_budget` is enabled, the
  scheduler first tries the target route budget, but refuses to drop
  route-neighbor anchors, bridge/type anchors, or direct bridge/provider/type
  definitions that satisfy a retained route-neighbor `via` edge. If the target
  budget is unsafe, it falls back to trimming only support/test/docs route
  noise; if that is still unsafe, it leaves the route untrimmed and records the
  reason. The scheduled-context metadata now reports `route_budget_applied`,
  `route_budget_reason`, `route_budget_target`, `route_budget_dropped`, and
  unsafe missing-core summaries, and the feedback table exposes `rb%`,
  `rb_drop`, `rb_unsafe%`, and `rb_miss`. A Python+Go q2 smoke at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35w_dynamic_budget_py_go_q2`
  reached `rec@5=1.000`, `files@5=1.000`, 16,493 mean tokens, and 4.8 mean
  turns across four cells. Dynamic budget applied only to Python bridge
  (`rb%=25%`, `rb_drop=1.0` overall), preserving `is_url_in_cache()` in the
  budgeted eight-node route. Both Go routes were marked
  `unsafe_missing_core` and left at 12 anchors, preserving `rec@5=1.000`.
  Conclusion: dynamic route budgeting is now auditable and conservative enough
  for more gates, but it is still not enabled by default because only one
  non-Python language has been checked and repeat variance remains open.
- M3.5x broadened dynamic route-budget validation to Python+Rust repeat gates
  and relaxed direct endpoint protection. Direct endpoints are no longer
  blanket must-keep anchors; route-neighbor anchors, bridge/type anchors, and
  `via` bridge/provider/type definitions remain protected. The Rust traceback
  route now budgets from 12 to 8 anchors while retaining
  `run_action_queue`/`add_iter` and the code-example endpoints. The repeat2
  gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35x_dynamic_budget_py_rust_repeat2_v2`
  reached `rec@5=1.000`, `files@5=1.000`, `rec_min=1.000`, 21,918 mean
  min-of-reps tokens, 3.5 mean turns, `rb%=50%`, `rb_drop=2.0`, and
  `rb_unsafe%=0%`. Conclusion: direct endpoint overflow can be budgeted safely
  when no bridge/type core is at risk, but this remains an opt-in scheduler
  mode until cost improves across languages.
- M3.5y tested a route-budget selector aligned with the dynamic must-keep rule
  for Go-like core routes. The selector actively re-inserted protected
  route-neighbor, bridge/type, and retained-`via` anchors by replacing weaker
  direct endpoints, so dynamic budget could keep core symbols instead of
  rejecting the whole route as `unsafe_missing_core`. A Go repeat1 audit before
  the selector change at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35x_go_unsafe_audit`
  preserved recall but left both routes untrimmed (`rb_unsafe%=100%`,
  `rb_miss=1.5`). After the change,
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35y_go_core_budget_audit`
  kept `rec@5=1.000`/`files@5=1.000`, trimmed both routes from 12 to 8
  anchors (`rb%=100%`, `rb_drop=4.0`, `rb_unsafe%=0%`), and reduced the
  traceback case from 4 to 3 turns. The bridge case still showed token
  variance upward, so this was treated as a mechanism probe rather than a
  default-enable pass.
- M3.5z failed the broader default-promotion gate for the core-reinserting
  selector. The Python+Go+Rust repeat2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35z_core_budget_py_go_rust_repeat2`
  kept `rec@5=1.000`/`rec_min=1.000`, applied route budget to four of six
  folded routes (`rb%=67%`, `rb_drop=2.7`, `rb_unsafe%=0%`), and preserved
  Python/Rust recall, but Go became too costly: bridge stayed at 7/7 turns and
  traceback stretched to 7/8 turns. A Go unbudgeted repeat2 control at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35z_go_unbudgeted_repeat2_control`
  reached `rec@5=1.000` with 3/4/4/4 turns. Conclusion: forcing protected
  bridge/type anchors into a small route budget can remove endpoints that are
  more useful for Go navigation, so the selector must remain conservative.
- M3.5aa reverted core-anchor re-insertion while retaining the safer dynamic
  must-keep audit and missing-core metadata. Dynamic route budget now applies
  when the target budget naturally keeps route-neighbor, bridge/type, and
  retained-`via` anchors; if not, it leaves the full route intact and records
  `unsafe_missing_core` with the missing anchor summaries. The current-code
  Python+Go+Rust repeat2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35aa_safe_dynamic_py_go_rust_repeat2`
  reached `rec@5=1.000`, `files@5=1.000`, `rec_min=1.000`, 17,110 mean
  min-of-reps tokens, and 3.8 mean turns. Python bridge and Rust traceback
  still budgeted safely; Go bridge/traceback were explicitly left untrimmed
  (`unsafe_missing_core`) and retained recall. Go bridge remains high-variance
  across repeat gates, so the next work should audit finalization guard and
  read trajectory rather than tighten the route budget.
- M3.5ab fixed the Go bridge trajectory by tightening traceback-query
  classification. The old classifier treated `path` and `upstream` as
  standalone direction terms, so the Go bridge query's "architectural path" was
  misclassified as `traceback_route=True` and picked up traceback ranking/guard
  behavior. The fix removes `upstream` as a direction term and only lets `path`
  count when paired with a stronger direction term (`before`, `caller`, `flow`,
  `originate`, `origin`, or `where`). The Go bridge-only repeat2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ab_go_bridge_classifier_repeat2`
  reached `rec@5=1.000` with 4/4 turns, `fg%=0%`, and ordinary
  `traceback_route=False` `lsp_route` context. The Go q2 repeat2 check at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ab_go_q2_classifier_repeat2`
  kept both bridge and traceback at `rec@5=1.000`; traceback remained
  `traceback_route=True` with conservative `unsafe_missing_core` full-route
  behavior.
- M3.5ac promoted the classifier fix through the Python+Go+Rust repeat2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ac_classifier_py_go_rust_repeat2`.
  It reached `rec@5=1.000`, `files@5=1.000`, `rec_min=1.000`, 16,484 mean
  min-of-reps tokens, and 3.7 mean turns, improving on M3.5aa's 17,110 tokens
  and 3.8 turns. Go bridge is now a normal 8-node route with no finalization
  guard, while Go traceback stays full-route `unsafe_missing_core`; Python
  bridge still classifies as traceback because its query explicitly asks how a
  cached remote-resource request reaches cache-directory resolution after a
  `before` cache check, and no regression was observed.
- M3.5ad tested a runner-level read-range guard as a cost probe, not a
  promotion. The idea was to return only newly uncovered lines for expanded read
  windows and short-circuit fully covered rereads. The Go/Rust q2 repeat2 run at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ad_read_range_guard_go_rust_repeat2`
  was stopped after the first Go traceback cell regressed to 8 turns and
  `rec@5=0.5`. Conclusion: tool-layer read truncation changes the model's
  confirmation semantics; duplicate/overlap pressure should be measured first
  and optimized through route/read planning or prompt-level cues, not by
  withholding requested read content.
- M3.5ae is implemented as telemetry rather than behavior. The feedback
  aggregate now reports repeated read starts (`rrep`), overlapped read-window
  rate (`rovlp%`), and overlapped lines (`rovlp_l`) from each cell's
  `file_reads`. Re-aggregating M3.5ac surfaced the pressure directly:
  `rrep=0.5`, `rovlp%=13%`, and `rovlp_l=48.5` on the folded Python+Go+Rust
  gate. This gives future LSP-route iterations a measurable read-planning target
  without changing model-visible read semantics.
- M3.5af was deliberately not promoted. Two model-facing read-planning attempts
  both hurt the traceback route path. First, lowering generic traceback routing
  from three reads to two plus adding explicit read-window instructions failed
  in
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35af_read_plan_go_rust_repeat2`:
  the first Go traceback repeat regressed to 6 turns and `rec@5=0.5`. Keeping
  the read-window instructions only for traceback routes still failed in
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35af_traceback_read_plan_go_rust_repeat2`:
  Go traceback repeat 1 regressed to 8 turns and `rec@5=0.5` (repeat 2 reached
  8 turns and `rec@5=1.0` before the run was stopped). Conclusion: do not give
  Haiku command-like read-window plans. The next read-overlap work should
  change route anchor selection, guard timing, or internal auto-open behavior,
  not the model-visible read semantics.
- M3.5ag is implemented as phase-aware telemetry rather than behavior. Cell
  JSON now records read `tool_call_id`/`turn`, and the feedback aggregate can
  also reconstruct turns from old trace `tool_call` events. The report splits
  overlapped read lines into `rov_pre_l`, `rov_post_l`, and `rov_fg_l`, with
  same-turn reads treated as pre-scheduled-context because the scheduler injects
  context after tool calls in that turn. Re-aggregating M3.5ac on just Go/Rust
  q2 showed `rovlp_l=45.0`, with `rov_pre_l=12.5` and `rov_post_l=32.5`; the
  failed early-trigger probe moved all overlap post-route (`rov_post_l=42.9`).
  This shows timing alone is not the bottleneck: after route context, the model
  can still loop back over prior read windows.
- M3.5ah was deliberately not promoted. A model-facing route read-state tag
  probe marked route bullets that overlapped prior reads as already read.
  Broad tagging at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ah_read_state_tags_go_rust_q2_repeat2`
  kept `rec@5=1.000` and lowered Go/Rust q2 overlap to `rovlp_l=33.4`
  (`rov_post_l=20.8`), but Go bridge slowed to 6/5 turns. Restricting tags to
  traceback routes at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ah_traceback_read_state_tags_go_rust_q2_repeat2`
  lowered overlap further (`rovlp_l=21.9`) but regressed Rust traceback repeat
  1 to 6 turns and `rec@5=0.5`, yielding `rec_min=0.5`. Conclusion: read-state
  labels can reduce redundant reads, but they destabilize answer selection; the
  next slice should change internal route/auto-open candidate choice, not add
  more model-visible read labels.
- M3.5ai was also deliberately not promoted. An internal route-display filter
  removed route anchors that overlapped prior read windows from the model-facing
  route context while keeping raw route anchors available to the final guard.
  The Go/Rust q2 repeat2 run at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ai_route_filter_read_overlaps_go_rust_q2_repeat2`
  kept `rec@5=1.000`, `rec_min=1.000`, and lowered mean min-of-reps tokens to
  11,519, but it failed the overlap gate: `rovlp_l` rose from M3.5ac's 45.0 to
  58.1 and `rov_post_l` rose from 32.5 to 45.6. The Rust bridge cells alone
  repeated 148 post-route overlapped lines. Conclusion: blindly hiding
  already-read route anchors reduces prompt cost but does not reduce repeated
  reads; the next slice should target concrete route-loop patterns such as
  repeated same-file reads after a route, not just remove overlapping anchors.
- M3.5aj is implemented as telemetry rather than behavior. The feedback
  aggregate now classifies post-route overlap into contained rereads, same-start
  expansions, shifted overlaps, and low-novelty reads (`post_sub`, `post_exp`,
  `post_shift`, `post_low`). Re-aggregating the Go/Rust surface showed why the
  previous behavior probes were unstable: M3.5ac's remaining `rov_post_l=32.5`
  came from only a few large repeated windows, while route filtering raised
  shifted/low-novelty overlap instead of removing it. Runner read ledger entries
  now also carry read-window metadata (`start_line`/`end_line`) for trace
  replay, but this metadata is not inserted into the compact prompt.
- M3.5ak was deliberately not promoted. Three concrete post-route loop fixes
  were tried and rejected. Inline finalization guard context at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ak_inline_guard_go_rust_repeat2`
  lowered nothing reliably and destabilized Go: Caddy traceback repeat 2 hit
  182,290 tokens, and Caddy bridge repeat 2 hit 81,521 tokens. Keeping three
  raw read outputs in compact mode at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ak_keep3_go_rust_repeat2`
  made Caddy bridge 6 turns in both repeats and inflated the compact seed to
  about 9k chars. Adding read line ranges to the model-visible compact summary
  at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35al_compact_read_ranges_go_rust_rep1`
  regressed Caddy traceback to `rec@5=0.5` in 8 turns, so that prompt-facing
  change was reverted. Conclusion: do not make the route lifecycle louder and
  do not expose read-window bookkeeping directly to Haiku. The next slice
  should use read-window metadata offline to choose a non-model-visible
  scheduler/ranking change, then verify it before changing prompts.
- M3.5am is implemented as trace-only route/read memory instrumentation. The
  scheduled `lsp_route` metadata now records how many route anchors are already
  read-confirmed (`route_read_confirmed_count`), how many read-confirmed anchors
  are core endpoint/bridge/provider/type anchors, and how many core route
  anchors remain unread. It also stores short read-confirmed and unread-core
  anchor lists for replay/debug. The model-facing route context is unchanged,
  and the feedback report exposes these counts as `rread`, `rread_c`, and
  `runread_c`. This gives M3.5an enough offline evidence to decide whether the
  next change should affect route ranking, auto-open candidate choice, or guard
  timing without adding more prompt-facing read-state labels.
- M3.5an is implemented as a non-model-visible `lsp_route` auto-open ranking
  change plus replayable route/read diagnostics. The feedback aggregate now
  reconstructs `rread`/`rread_c`/`runread_c` for older traces that predate
  M3.5am metadata by replaying read tool calls, route locations, and
  auto-opened locations. Re-aggregating old Go/Rust M3.5ac/M3.5ai cells exposed
  that scheduled routes often had several unread core anchors left
  (`runread_c` about 3.2-3.5), so the auto-open selector now promotes
  multi-line `route_neighbor` endpoint/bridge/provider anchors before ordinary
  direct endpoints. If a `route_neighbor` is represented by a short provider
  but points through `via=...`, the selector promotes the corresponding direct
  bridge/provider/type definition instead. Type-heavy and traceback-specific
  ranking paths keep their existing ordering.

  The Go/Rust smoke at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35an_route_neighbor_autoopen_go_rust`
  completed 8/8 cells. `preinj_graph_scheduled` reached `rec@5=1.000`,
  `files@5=1.000`, 20,568 tokens, 3.5 turns, `rread=4.5`,
  `rread_c=4.5`, and `runread_c=1.5`, a paired `SAVE` versus grep
  (`+0.500` rec@5, `-101044` tokens, `-7.5` turns). The follow-up Go-only
  smoke after bridge-via ranking at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35an_route_bridge_via_go`
  completed 4/4 cells; scheduled route stayed `rec@5=1.000` in 4 turns and
  22,651 tokens (`SAVE`, `+0.333` rec@5, `-123804` tokens, `-7` turns). In
  that Caddy trace, the model had already read `replacer.go` before the route
  fired, so auto-open correctly skipped the `route_neighbor` provider and its
  `NewReplacer` bridge, then opened an unread direct endpoint. That is the
  desired read-ledger behavior, not a missed promotion.
- M3.5ao is implemented as the narrow repeat gate and answer-cited unread-route
  diagnostic for M3.5+ route scheduler work. A reusable config now lives at
  `scripts/agent_compile/configs/haiku_route_scheduler_probe.yaml`; it runs only
  `grep_only` and `preinj_graph_scheduled` over Go/Rust traversal bridge and
  traceback queries, so route lifecycle changes can be repeat-checked without
  paying for unrelated eager/fanout arms. The repeat2 run at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ao_route_scheduler_go_rust_repeat2`
  completed 16/16 cells. Folded across 4 queries, scheduled route reached
  `rec@5=1.000`, `files@5=1.000`, 14,005 tokens, and 3.5 turns, versus
  `grep_only rec@5=0.438`, 133,717 tokens, and 11.5 turns; the paired delta was
  `+0.562 rec@5`, `-119712` tokens, and `-8.0` turns (`SAVE`). Both Caddy
  bridge and traceback repeated at `rec@5=1.000` in 4 turns; both Ruff bridge
  and traceback repeated at `rec@5=1.000` in 3 turns.

  The feedback report now also includes `runans` and `runans5`, which count
  unread route-core anchors that are nevertheless cited by the final answer,
  overall and within the first five answer spans. It also reports `runr5` and
  `runrtail`, splitting those answer-cited unread anchors by whether they were
  already in the first five route locations. On the M3.5ao repeat2, scheduled
  route had `runread_c=4.5` but only `runans=0.8`, `runans5=0.8`, `runr5=0.5`,
  and `runrtail=0.2`. The remaining unread core anchors are therefore mostly
  unneeded route-tail candidates, not an argument for opening more source by
  default. Do not raise traceback auto-open anchors solely to reduce
  `runread_c`; require a named precision/order failure first.
- M3.5ap added a scheduled-only Haiku smoke config at
  `scripts/agent_compile/configs/haiku_route_scheduler_scheduled_only.yaml` for
  route-only iteration after a paired baseline has already established the grep
  comparison. The prompt-order diagnostic showed that sorting traceback route
  anchors could replay-improve route visibility on old traces (`runr5` from
  `0.50` to `0.75`, `runrtail` from `0.25` to `0.00`), but the live opt-in
  smoke at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ap_prompt_order_go_rust`
  exposed a real regression: Ruff traceback dropped to `rec@5=0.5` and 6 turns
  while the other three cells stayed green. The prompt-order helper is therefore
  gated behind `graph_read_symbol_traceback_route_prompt_order` and remains
  disabled by default. The restore smoke at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ap_default_restore_go_rust`
  completed 4/4 scheduled-only cells with `rec@5=1.000`, 22,196 tokens, 3.5
  turns, `runans=1.0`, `runr5=0.8`, and `runrtail=0.2`. Treat this as a
  negative-result milestone: route order is a useful diagnostic, but default
  prompt reordering needs a stronger repeat gate before promotion.
- M3.5aq tightened the read-confirmed top-anchor correction loop instead of
  promoting prompt reordering. The top-anchor audit trace/report now separates
  total missing priority anchors (`taud_m`) from anchors that were missing
  because they were not read (`taud_u`). On the failed M3.5ap prompt-order run,
  the aggregate showed `taud%=25%`, `taud_m=0.2`, and `taud_u=0.0`, proving the
  Ruff traceback miss was not a read failure; the model had read/final-guarded
  `add_iter` but left it out of the final top-5 Locations. The correction prompt
  now treats missing read-confirmed route anchors as required top-route anchors
  and explicitly tells the model to place them before optional wrapper/helper or
  smaller interior ranges.

  A dedicated opt-in probe now lives at
  `scripts/agent_compile/configs/haiku_route_prompt_order_probe.yaml`. It keeps
  `graph_read_symbol_traceback_route_prompt_order` out of the default probe and
  runs only the Ruff traversal bridge/traceback pair. The M3.5aq run at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35aq_prompt_order_required_guard_ruff`
  completed 2/2 cells with `rec@5=1.000`, 8,347 tokens, 3.0 turns,
  `runr5=1.0`, and `runrtail=0.0`; it did not trigger the top-anchor correction
  in that sample, so this is a green risk probe, not enough evidence to enable
  prompt reordering by default. Promotion still requires a repeat2 pass against
  the Go/Rust route scheduler gate.
- M3.5ar added
  `scripts/agent_compile/configs/haiku_route_prompt_order_gate.yaml` and ran
  the scheduled-only Go/Rust traversal repeat2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ar_prompt_order_gate_go_rust_repeat2`.
  The opt-in path completed 8/8 cells with `rec@5=1.000`, but it failed the
  promotion gate on cost/stability: two traceback reps required top-anchor
  correction, both were read-confirmed misses (`taud_u=0.0`), and `turn_hi`
  rose to `4.8` versus the M3.5ao scheduled route repeat where turns were
  stable at `3.5`. Keep `graph_read_symbol_traceback_route_prompt_order`
  disabled by default.
- M3.5as tested a cheaper answer-only top-anchor correction for
  read-confirmed misses. The feedback report now includes `taud_ao%` so this
  path can be tracked explicitly. A live rep1 smoke at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35as_answer_only_prompt_order_go_rust_rep1`
  showed the risk clearly: answer-only correction reduced the Caddy traceback
  turn count to 4, but the corrected answer scored `rec@5=0.0`. The answer-only
  correction path is therefore opt-in only via
  `graph_schedule_answer_only_correction`; the default remains the safer
  tool-loop correction.
- M3.5at split read-confirmed top-anchor misses into two different failure
  modes. A bounded 2-turn tool-loop retry at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35at_bounded_top_anchor_go_rust_rep1`
  was not safe: the Caddy traceback cell triggered
  `correction_mode=bounded_tool_loop`, dropped to `rec@5=0.0`, and ended while
  the model was still reading/grepping. Bounded correction is therefore opt-in
  only via `graph_schedule_bounded_top_anchor_correction`, and the feedback
  report tracks it as `taud_bt%`.

  The safe default optimization is narrower: deterministic answer patching only
  reorders a read-confirmed required anchor that is already present after the
  first `top_k` `Locations:` entries. It no longer injects absent anchors. The
  report tracks this as `taud_dp%`. The initial deterministic repeat2 exposed
  the bug in the broader injection rule: Caddy traceback rep1 fell to
  `rec@5=0.5` when the required anchor was absent from the answer. After
  tightening patch applicability, the Caddy-only repeat2 at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35at_top_anchor_patch_caddy_repeat2`
  completed 4/4 cells at `rec@5=1.000`; the absent-anchor traceback rep fell
  back to `correction_mode=tool_loop` and recovered to `rec@5=1.000`, at the
  expected cost of 7 turns. This keeps tail reorder cheap without pretending it
  solves missing evidence.
- M3.5au tested an opt-in read-only top-anchor correction for absent
  read-confirmed anchors. The hypothesis was that a correction runner exposing
  only `read` could avoid grep/bash drift while costing less than the full tool
  loop. The feedback report now tracks this path as `taud_ro%`, and the probe
  config lives at
  `scripts/agent_compile/configs/haiku_read_only_top_anchor_probe.yaml`. The
  Caddy repeat2 at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35au_read_only_top_anchor_caddy_repeat2`
  rejected the hypothesis: both traceback reps triggered
  `correction_mode=read_only_tool_loop`, both ended at `rec@5=0.5`, and the
  aggregate dropped to `rec@5=0.750`. Read-only correction stays opt-in only via
  `graph_schedule_read_only_top_anchor_correction`. The durable lesson is that
  absent-anchor misses need either full retry or a validated cascade; simply
  removing search tools is cheaper but not correct.
- M3.5av tested that validated cascade explicitly. Read-only correction now
  runs through `scheduled_top_anchor_required_anchor_audit`; if the corrected
  answer still omits the required anchor from the top-k `Locations`, the harness
  falls back to a fresh full tool-loop correction and records
  `cascade_fallback`/`validation_missing_count`. The Caddy repeat2 at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35av_read_only_cascade_caddy_repeat2`
  recovered correctness (`rec@5=1.000`, `files@5=1.000`, `rec_min=1.000`), but
  both traceback reps cascaded from read-only to full retry and cost 9/10 turns
  with about 50k tokens each. Conclusion: the validation guard is useful
  substrate and `taud_fb%` is now observable, but read-only-before-full is not a
  promotion path on this failure shape.
- M3.5aw makes route-core evidence more explicit without changing the model
  prompt. The route/read metadata now reports protected route-core anchors
  (`rkeep`/`runkeep`) using the same rule as dynamic route-budget must-keep:
  route-neighbor anchors, bridge/type anchors, and retained `via`
  bridge/provider/type definitions. The feedback aggregate can reconstruct
  these counts from old trace `locations` plus read ranges. Focused unit tests
  cover provider-only top-anchor suppression, bridge-only correction
  suppression, metadata counts, and old-trace reconstruction; re-aggregating
  the M3.5av Caddy run exposed `rkeep=3.0` and `runkeep=1.0` for scheduled
  route cells. A tiny Caddy scheduled-only Haiku smoke at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35aw_bridge_priority_caddy_rep1`
  completed 2/2 traversal cells with `rec@5=1.000`, `files@5=1.000`,
  `rec_min=1.000`, 13,254 mean min-of-reps tokens, 4.0 mean turns,
  `taud%=0%`, `rkeep=2.0`, and `runkeep=2.5`. This is a green mechanism smoke,
  not a default-promotion gate; the next repeat gate should include Ruff.
- M3.5ax rejected bridge/factory anchors as default top-anchor correction
  priorities while preserving the protected-core telemetry. The Go/Rust
  scheduled-only repeat2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ax_route_core_go_rust_repeat2`
  completed 8/8 cells with `rec@5=1.000`, `files@5=1.000`,
  `rec_min=1.000`, 17,690 mean min-of-reps tokens, and 3.5 mean turns, but the
  experimental bridge-priority audit raised `taud%=12%`: Caddy bridge rep1
  triggered a full `tool_loop` correction and stretched to 7 turns. The default
  top-anchor correction therefore remains endpoint/type or final-guard driven;
  bridge evidence is tracked through `rkeep`/`runkeep` and should influence
  future route selection or auto-open planning, not post-answer correction.
- M3.5ay moved that signal into route planning instead of correction. For
  bridge-heavy routes or replacement/template queries, auto-open now promotes
  unread protected direct bridge/provider/type anchors ahead of generic direct
  endpoints, while still keeping route-neighbor core anchors first and skipping
  spans already covered by prior reads. This is a non-prompt change: the route
  context text and post-answer audit contract are unchanged. Unit coverage pins
  the Caddy-shaped case where `Replacer#ReplaceAll` must auto-open before a
  weaker `NetworkAddress#JoinHostPort` endpoint when both are unread. The
  Go/Rust scheduled-only repeat2 gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ay_protected_bridge_autoopen_go_rust_repeat2`
  completed 8/8 cells with `rec@5=1.000`, `files@5=1.000`,
  `rec_min=1.000`, 11,645 mean min-of-reps tokens, 3.5 mean turns,
  `turn_hi=3.5`, `taud%=0%`, `rkeep=2.2`, and `runkeep=0.5`. Caddy bridge was
  stable at 4/4 turns with no correction; in that live trace the protected
  bridge anchors were already read before route auto-open, so the route still
  auto-opened the endpoint. Conclusion: protected-core auto-open ordering is a
  safe default mechanism, but further cost work should use read trajectory and
  route timing evidence rather than making bridge anchors correction targets.
- M3.5az tested an earlier generic-symbol route trigger for bridge/replacement
  queries and rejected it as a default. The Caddy bridge trace showed the only
  remaining pre-route cost was waiting for the fourth generic read, so an
  opt-in `graph_bridge_read_symbol_min_reads` gate was added for experiments,
  but the default stays at four generic reads. The opt-in repeat2 at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35az_bridge_early_go_repeat2`
  moved Caddy bridge to `rturn=1.5`, `rcalls=3.0`, `rgmin=3.0`, and
  `rbridge%=100%`, but regressed to `rec_min=0.667`, `turn_hi=12.0`, and
  `tok_rng=33904`. The default-restored Caddy rep1 at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35az_default_restored_caddy_rep1`
  recovered `rec@5=1.000` in 4 turns with `rturn=3.0`, `rcalls=4.0`, and
  `rgmin=4.0`. The feedback aggregate now reports `rturn`, `rcalls`, `rgmin`,
  and `rbridge%` so future route work can distinguish premature route injection
  from post-route read-plan failure. Conclusion: do not optimize this cluster by
  making `lsp_route` fire earlier; optimize how the agent reads and converges
  after the stable route injection.
- M3.5ba initial probe tested whether explicit LSP-shaped tools plus stronger
  prompt guidance are enough without scheduler policy. The baseline LSP facade
  sample at `/tmp/codeminer_haiku_lsp_facade_m35ba_go_q1_rep1` showed
  `lsp_graph` did not call any `lsp_*` tool and regressed to `rec@5=0.333`.
  A prompt-only experiment at
  `/tmp/codeminer_haiku_lsp_facade_m35ba_prompt_route_go_q1_rep1` made the
  model call `lsp_route`, but only as the 15th tool call, after broad
  BM25/read/glob exploration; the aggregate now surfaces this as `lr%=100%`,
  `lrturn=9.0`, and `lrres=12.0`. It still finished at 12 turns and
  `rec@5=0.667`, far behind the scheduled route path. The prompt-only change was
  not retained. Conclusion: LSP facade tools are useful primitives, but
  CodeMiner's harness advantage comes from deterministic route timing and
  post-route read-plan policy, not from hoping the model chooses the route tool
  early.
- M3.5ba is now implemented as a scheduler lifecycle fix. A Caddy bridge probe
  at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ba_bridge_guard_default_caddy_rep1`
  exposed a separate pre-route failure: after one read, generic
  `search_calls` pressure fired at turn 2 and consumed the scheduler's one-shot
  opportunity before the stable four-read `lsp_route` gate could run. That cell
  finished at `rec@5=0.333` in 12 turns with `route%=0%`. The scheduler now
  holds generic fanout for route-shaped traceback/bridge queries when at least
  one read has happened but the read-symbol route threshold has not been met
  yet. The restored Caddy gate at
  `/tmp/codeminer_haiku_static_lsp_route_scheduler_m35ba_fanout_hold_caddy_rep1`
  reached `rec@5=1.000`, `files@5=1.000`, 22,637 tokens, and 4.0 turns with
  `route%=100%`, `rturn=3.0`, `rcalls=4.0`, `rgmin=4.0`, `rbridge%=100%`, and
  `fg%=0%`. The earlier bridge finalization-guard hook remains available for
  post-route drift, but this Caddy failure was fixed by preventing pre-route
  fanout from stealing the scheduler slot.
- M3.5bb instrumentation is in place for the route-lifecycle repeat gate. The
  scheduler now records a non-model-visible fanout-hold counter only when a
  route-shaped bridge/traceback query has started reading, has not yet reached
  the stable generic `lsp_route` read threshold, and ordinary `search_calls`
  fanout would otherwise trigger. Sweep cells persist the hold count, first hold
  turn, read-call count, search-call count, generic read threshold, and hold
  reason. Feedback reports expose the main gate columns as `rhold`, `rhturn`,
  and `rhcalls` beside `rturn`/`rcalls`/`rgmin`, so M3.5bb can prove search
  fanout was held rather than merely absent from the final route trace. The
  aggregate also writes `route_lifecycle_gate.json` and renders a Route
  Lifecycle Gate table for the M3.5bb criteria: scheduled route arm present,
  at least four folded Caddy/Ruff route queries, repeat2 coverage for every
  folded scheduled row, paired grep coverage for those queries, `rec_min=1.000`,
  `route%=100%`, `sfan%=0%`, `taud%=0%`, and `rgmin>=4`. Promotion still
  requires the Caddy/Ruff scheduled-route repeat2 gate to pass that
  machine-readable gate. The aggregate CLI now supports
  `--require-route-lifecycle-gate`, which writes the normal artifacts first and
  then exits non-zero if the M3.5bb gate does not pass. It also supports
  `--preflight-route-lifecycle-config`, with explicit synthesis/category/query
  selector arguments, so the Caddy/Ruff repeat2 run can fail fast before
  spending Haiku calls when the config lacks paired baseline, repeat2 coverage,
  traversal selection, or the explicit `rgmin>=4` read threshold. The route
  scheduler probe config now sets `graph_generic_read_symbol_min_reads: 4` to
  make the planned run match the hard gate. The preflight path is intentionally
  lightweight: it lazy-loads answer diagnostics and trace summaries so the
  config check reads only YAML/config code and does not import the retrieval
  evaluator, FAISS, or index stack. Re-aggregating the old green M3.5ao paired
  repeat2 with this hard gate produces
  `status=incomplete`, not `fail`: recall, route usage, top-anchor correction,
  query coverage, repeat coverage, and paired baseline coverage are all green,
  but the artifact predates `rgmin` instrumentation. Re-aggregating the M3.5ay
  scheduled-only repeat2 is also `incomplete` because it has no paired grep
  baseline. The next promotion action is therefore a fresh Caddy/Ruff repeat2
  run with current cells, not a scheduler-policy change.
- M3.5bc adds the frozen feedback-suite contract needed to stop route
  lifecycle work from drifting across ad hoc smokes. The reusable suite
  manifest at
  `scripts/agent_compile/feedback_suites/haiku_static_lsp_route_q2.yaml`
  covers Python/Go/Rust traversal, two queries per instance/category, two
  arms, and two reps for a bounded 24-cell Haiku gate. The companion planner,
  `scripts/agent_compile/plan_feedback_suite.py`, validates the manifest
  against the sweep config, promotion profile registry, expected query count,
  and max-cell budget, then emits the preflight, sweep, and aggregate commands
  plus `feedback_suite_plan.json`. This is now the default route-lifecycle
  iteration surface before adding more LSP facets or changing scheduler
  defaults.
- M3.6 started the external-agent baseline suggested by the runner audit. The
  MCP server now exposes `lsp_route` over the same static symbol graph used by
  the runner skill, and `scripts/agent_compile/aggregate_external_agent_baseline.py`
  scores Claude/Codex JSONL with the same `answer_blocks` overlap metric as the
  CodeMiner sweeps. On the Caddy bridge query, direct MCP route verification
  returned the expected `doActiveHealthCheckForAllHosts`, `NewReplacer`, and
  `globalDefaultReplacements` anchors. External agent results at
  `/tmp/codeminer_external_agent_baseline` show the loop gap clearly:
  `claude_opus_mcp` reached `rec@5=1.000`, but needed 8 turns and called
  `lsp_route` as its 5th tool; `claude_haiku_mcp` needed 23 turns, called
  `lsp_route` as its 18th tool, and scored `rec@5=0.333`; the original
  `codex_mcp_cancelled` run scored `rec@5=0.333` with 19 tools, and all four
  MCP calls were cancelled by the Codex runtime before results arrived. A
  policy-isolated rerun, `codex_mcp_bypass`, used
  `--dangerously-bypass-approvals-and-sandbox`, completed all CodeMiner MCP
  calls with zero cancellations, called `lsp_route` as its 6th tool, and used
  only 9 tools / 926 output tokens. Its strict `rec@5=0.667` is a final-answer
  schema failure rather than a route failure: the answer named the correct
  `doActiveHealthCheckForAllHosts`, `NewReplacer`, and
  `globalDefaultReplacements` anchors, but reported `healthchecks.go:242-298`
  without the repo-relative path
  `modules/caddyhttp/reverseproxy/healthchecks.go`. Conclusion: exposing the
  LSP-shaped graph tool is necessary for fair external comparison, but the main
  CodeMiner harness advantage is still route timing, read-plan convergence, and
  final answer schema control.
- M3.6b tightened that conclusion. A schema-constrained Codex bypass run,
  `codex_mcp_bypass_schema`, completed with zero MCP cancellations, called
  `lsp_route` as its 3rd tool, and scored `rec@5=0.667` but `rec@all=1.000`
  with six parsed answer spans. The answer was semantically accurate: it named
  the health-check endpoint, `caddy.Replacer`, `NewReplacer`, `ReplaceOrErr`,
  `Replacer.Get`, and `globalDefaultReplacements`. The remaining @5 loss is
  not a route failure; it is a deliverable-ranking issue. The provider span was
  present but appeared after helper spans, so it fell outside top-5. The
  external baseline table now reports `rec@all`, parsed span count, and an
  `answer_diagnosis` label to distinguish "not found", path/schema alias gaps,
  and "found but ranked/structured badly"; in the current Caddy table,
  `codex_mcp_bypass` is `path_alias_gap`,
  `codex_mcp_bypass_schema` is `rank_gap`, the two green runs are `ok`, and
  the weaker Haiku/Codex-cancelled runs are `content_gap`. Synthesis sweep
  cells now persist `answer_span_count`, `answer_top_spans`, and
  `answer_all_metrics` for the same feedback loop. The feedback aggregate now
  reports strict `rec@5` beside deduped `rec@all` and path-alias `alias@all` in
  the answer diagnosis plan, so route/content misses can be separated from
  final-answer ranking, path, and schema friction without loosening the scorer.
  The run also exposed two harness-boundary bugs that are now fixed:
  `parse_answer_spans` did not parse backtick-only multiline `Locations:`
  continuations, and MCP `lsp_route` leaked internal 0-based graph line numbers
  instead of 1-based agent-facing locations. CodeMiner's runner should keep
  enforcing a structured final span list instead of relying on Markdown prose.
- M3.6b's narrow schema gate now passes on the Caddy bridge cell. After the
  MCP line-origin fix, `codex_mcp_bypass_schema_gate` used the same Codex graph
  MCP setup with an explicit endpoint/bridge/provider final contract. It
  completed with zero MCP cancellations, called `lsp_route` as its 7th tool,
  emitted exactly three parsed spans, and scored `rec@5=1.000` / `rec@all=1.000`.
  The resulting `Locations:` line was the intended structured deliverable:
  `modules/caddyhttp/reverseproxy/healthchecks.go:243-299`,
  `replacer.go:29-38`, and `replacer.go:297-337`. This does not make the
  external loop competitive with CodeMiner's scheduled route timing yet (Codex
  still spent 9 tools and used broad BM25/rg before the route), but it proves
  the remaining Caddy failure was contract/order control, not graph route
  recall. The external baseline aggregator now counts same-stem `.stderr`
  `Internal Server Error` occurrences as `stderr_err`; the green schema gate
  still emitted 12 such Codex MCP log lines despite successful JSONL tool
  completions, so the noise is tracked but not yet root-caused. A local
  `command -v opencode` check found no installed OpenCode binary, so OpenCode
  remains pending until there is a reproducible binary or wrapper. The external
  baseline aggregator now has a matching opt-in hard gate:
  `aggregate_external_agent_baseline.py --require-run-gate <label>` writes the
  normal report first, then exits non-zero unless the named run meets the
  configured `rec@5`, `answer_diagnosis`, MCP cancellation, stderr-noise, and
  optional `lsp_route`/native-LSP requirements. This turns the Caddy schema gate
  and future native-IDE/LSP checks into reproducible CLI assertions rather than
  table-reading.
- The strict final-answer contract is now shared by the base `AgentRunner`
  system prompt, last-turn salvage prompt, and forced schema turn. It no longer
  asks only for three labels; it also requires repo-relative paths, at most five
  `Locations` entries, and route-importance ordering
  endpoint/caller -> bridge/factory -> provider/value/type before helper or
  interior ranges. This brings ordinary runner finalization closer to the
  green `codex_mcp_bypass_schema_gate` deliverable and reduces reliance on
  post-hoc parser tolerance.
- M3.6b also has a minimal Codex MCP policy repro. Under
  `codex --ask-for-approval never exec --json --sandbox read-only`, a prompt
  that only called CodeMiner `get_manifest` produced a completed JSONL turn
  whose MCP item failed with `user cancelled MCP tool call`. The same prompt
  under `--dangerously-bypass-approvals-and-sandbox` completed successfully, and
  a second minimal bypass prompt completed `lsp_route` with the expected Caddy
  anchors. Codex still logs repeated MCP exception-handler
  `Internal Server Error` messages on stderr even when the JSONL tool calls
  complete with `error=null`; treat that as a separate transport/noise issue.
  For now, Codex graph-MCP baselines need an explicit policy mode in the run
  label, and default read-only/no-approval `codex exec` is not a valid
  no-cancellation MCP comparison.
- This is not the same as a native IDE/LSP baseline. Claude Code's local CLI
  advertises `--ide`, and its `--bare` mode explicitly skips LSP, so native
  Claude Code + IDE/LSP should be measured separately. The local Codex CLI
  surface checked here exposes MCP/app-server/shell entry points but no direct
  `--ide`/LSP flag in `codex exec`; Codex native LSP, if available through an
  editor surface, needs a separate reproducible harness. Keep these baselines
  distinct: native IDE/LSP measures external-agent language-server behavior,
  while CodeMiner MCP measures whether external agent loops can exploit our
  static whole-repo graph route.
- A first `claude_haiku_native_ide` attempt at
  `/tmp/codeminer_external_agent_baseline/claude_haiku_caddy_native_ide.jsonl`
  is recorded only as an environment check, not a promoted native-LSP result:
  although invoked with `--ide`, the init tool list contained only the default
  file/search tools and no IDE/LSP navigation surface. It finished the Caddy
  query at `rec@5=0.667` in 13 turns with 12 tools. The native-LSP baseline
  therefore remains open until we run in an environment with an attached IDE/LSP
  surface and can see definition/reference tools in the trace.
- M4.1 has started with a replay-adjacent trace summary layer. The first trace
  artifacts carried `schema_version=1`; new synthesis cells persist
  `trace_summary`; and
  `scripts/agent_compile/summarize_trace.py` can load an existing cell JSON from
  disk and summarize turns, tool mix, read paths, context ledger states,
  compaction, scheduled `lsp_route`, and audit events without raw stdout. On the
  restored Caddy M3.5ba cell, the CLI reports `answer_rec@5=1.0`, 4 read tools,
  one scheduled `lsp_route`, and one compaction event from the persisted cell
  alone. The next slice linked committed/scored answer spans back to trace
  evidence: new cells persist `answer_span_evidence`, and the summary reports
  both trace-evidenced spans and stricter read-confirmed spans. On the same
  Caddy cell, the old artifact now summarizes as `8/8 trace-evidenced` and
  `7/8 read-confirmed`, which separates a graph-route anchor from file-read
  evidence instead of collapsing both into an opaque recall score. The feedback
  aggregate now carries the same evidence stratification as `ans`, `te%`, `rc%`,
  and `unc`; rerunning it over the restored Caddy cell reports `ans=8.0`,
  `te%=100%`, `rc%=88%`, and `unc=0.0`. This closes the "can I inspect a cell
  artifact after the run?" gap, the first evidence link, and the first aggregate
  source/state stratification. A fourth slice adds
  `scripts/agent_compile/summarize_trace.py --format replay-markdown`, which
  reconstructs a deterministic session timeline from the persisted cell trace.
  On the restored Caddy cell the replay shows the actual loop: initial
  healthchecks/hosts reads, compaction, two `replacer.go` reads, scheduled
  `lsp_route`, then the final answer. This is now enough to audit loop timing
  without raw stdout, though exact model-message replay is still out of scope.
  A fifth slice makes that boundary machine-readable: summaries and replay
  JSON now include `replay_readiness`, with explicit blockers such as
  `missing_message_transcript`, `assistant_content_not_persisted`,
  `tool_result_content_not_persisted`, and incomplete tool envelopes. Trace
  summaries also report `context.source_states`, so aggregate debugging can
  separate preload/read/scheduled-route evidence without scraping raw events.
  The sixth slice promotes new trace artifacts to `schema_version=2`: the runner
  records an append-only `message_replay` transcript, a deduplicated
  `tool_schema_replay` table, and `llm_call` events that point at the exact
  replay-message and tool-schema indexes visible to each model call. This
  preserves early assistant/tool-result content even when compact mode resets
  the live context to a distilled seed. A seventh slice turns replay readiness
  into an opt-in gate: `scripts/agent_compile/summarize_trace.py
  --require-exact-replay` writes the requested summary/replay output first,
  then exits non-zero if `replay_readiness.exact_message_replayable` is false
  and reports the concrete blockers. This lets promoted schema-v2 artifacts
  prove exact replayability directly while old partial traces remain
  inspectable.

Next autonomous slices:

| Slice | Work | Promotion Gate |
| --- | --- | --- |
| M2.3 | Tune compact seed/tail policy from trace rows if tokens remain high | First feedback gate is wired and actionable: aggregate reports compact prune ratio, tail share, kept-read count, a policy hint (`hold`, `try_keep1`, `trim_tail`, etc.), and writes `compact_policy_plan.json`; `compact_keep_reads` and `compact_tail_chars` can be set per preload arm; promotion still requires a Haiku probe showing lower seed/tail cost without lowering read-confirmed recall |
| M3.3d | Tighten scheduled graph/LSP routing to thin-context or contradictory-context cases | Beat grep without regressing compact/post-run graph fan-out; `sched%` and `sskip%` must explain all pressure events |
| M3.5bb | Route-lifecycle repeat gate after fanout hold | Caddy/Ruff repeat2 has at least four folded route queries, repeat2 scheduled coverage, paired grep coverage, `rec_min=1.000`, `taud%=0%`, `rgmin=4.0`, and no `search_calls` scheduler preemption on route-shaped bridge/traceback queries |
| M3.5bc | Frozen route-lifecycle feedback suite | `plan_feedback_suite.py` validates `haiku_static_lsp_route_q2.yaml`, emits preflight/sweep/aggregate commands, and preserves a 24-cell Python/Go/Rust scheduled-route gate with route-lifecycle promotion profile |
| M3.6a | Native IDE/LSP external-agent baseline | Claude Code `--ide` run is captured on Caddy/Ruff with tool trace and `answer_blocks@5`; Codex native LSP is included only through a reproducible IDE/app-server surface, not assumed from MCP |
| M3.6b | Codex/OpenCode graph-MCP schema hardening | Caddy Codex bypass schema gate is green (`answer_blocks@5=1.000`), answer failures are classified by `answer_diagnosis`, and stderr MCP exception noise is quantified as `stderr_err`; remaining promotion work is OpenCode if an installed binary or reproducible wrapper becomes available |
| M3.7 | Add additional static-LSP facets only where route traces prove a gap (`symbols`, lightweight hover, or references-by-role) | A new facet must reduce reads, turns, or corrections on a named failure cluster; do not add facets with no trace-backed use |
| M4.1 | Persist run traces as replayable session artifacts | Gates are green: a completed cell can be loaded from disk, summarized without raw stdout, linked from answer spans to trace/read-confirmed evidence, aggregated by trace/read evidence coverage, replayed as a deterministic event timeline, checked for exact replay blockers via `replay_readiness`, and persisted with append-only `message_replay`, `tool_schema_replay`, and per-call indexes for both |
| M5.1 | Add a typed tool envelope for read/grep/bash outputs | First gates are green: runner tool records, trace events, context ledger entries, and synthesis tool summaries carry a uniform status/error/truncation/duration envelope; file/shell tools record permission/sandbox metadata and bash outcomes classify nonzero exits, spawn failures, timeouts, and output truncation |
| M5.2 | Add edit-run diff/revert audit artifacts | First gates are green: a clean or dirty-start git worktree can be wrapped with before/after diffs, preimages, generated revert script, a recorded verification command, a concrete audited edit-command runner/CLI, and an audited `AgentRunner.run` surface that persists `agent_result.json` with tool envelope metadata |

## Iteration Details

### Feedback Target: Haiku Probe

Use the small Haiku probe before any full sweep:

```bash
python scripts/agent_compile/run_synthesis_sweep.py \
  --config scripts/agent_compile/configs/haiku_feedback_probe.yaml \
  --output-dir results/agent_compile/haiku_feedback_probe \
  --synthesis-configs Python,Go,Rust \
  --categories behavioral,symbol_hint,traversal \
  --max-queries-per-category 1
python scripts/agent_compile/run_feedback_iteration.py \
  --output-dir results/agent_compile/haiku_feedback_probe \
  --promotion-profile haiku_compact
```

Default size is 27 cells: 3 repos x 3 categories x 3 arms. Increase
`--max-queries-per-category` to 2 only after a change looks promising. The
scorecard reports committed answer recall, tokens, turns, format failures,
successful reads, repeated/overlapped read windows split by route/guard phase,
post-route read-loop shapes, route read-confirmation memory,
rejected context, preload offered spans, preload verification rate, fallback
rate, graph expansion rate, LSP facade adoption, scheduled static-LSP trigger
rate, route fanout-hold behavior, scheduled route/final-guard usage, scheduled
answer audits, compaction rate, and no-read rate. A harness change should first
pass this probe before it is promoted to the larger compact or synthesis
sweeps.

Iteration gate:

- keep a change when the pooled `preinj_eager_compact` delta is not worse than
  grep by more than 0.02 answer-block recall and reduces tokens or turns;
- use `--promotion-profile haiku_compact` as the default local promote check;
  it expands to the feedback iteration gate with savings required plus the
  answer evidence and replay-readiness gates, writes
  `promotion_decision.json` and `promotion_profile_gate.json`, and preserves the
  underlying gate artifacts for diagnosis;
- the aggregate CLI now codifies this with
  `--require-feedback-gate <arm>` and optional
  `--feedback-gate-require-savings`; it writes
  `feedback_iteration_gate.json` and exits non-zero after writing artifacts
  when the ALL-row baseline is missing, `rec@5` regresses beyond 0.02, required
  savings are absent, or a category row regresses;
- reject or revise when behavioral/traversal regresses even if symbol-hint
  improves; the hard gate checks category rows so pooled wins cannot hide
  targeted failures;
- require promoted answers to remain auditable with
  `--require-answer-evidence <arm>`; this writes `answer_evidence_gate.json`
  and fails when an arm has no parsed answer spans, a committed span has no
  trace evidence, or an optional `--answer-evidence-min-read-rate` threshold is
  missed;
- inspect trace rows before changing retrieval: high `no_read` points to runner
  steering, high `rejected` points to context quality, and high tokens after
  compaction points to compaction/tail policy.

### P0: Experiment Hygiene

Exit criteria:

- compact-preload aggregate output refuses to generate a partial report when a
  required sweep is missing;
- historical result directory aliases are encoded in the script, not tribal
  memory;
- reports include or preserve `paired_n`, `format_failed`, and local-model
  `cost_usd` nullability where relevant;
- partial smoke directories are clearly separated from headline result sets.

### P1: Durable Run Trace And Context Ledger

Add a run trace schema that records model messages, tool calls, tool results,
usage, compaction events, and final answer evidence. Add a context ledger that
tracks each context item by source, path, span, score, freshness, and state:
offered, read, rejected, verified, summarized, or cited.

First slice shipped: trace dictionaries include a schema version, synthesis
cells persist `trace_summary`, and `scripts/agent_compile/summarize_trace.py`
can summarize old cell JSON files without rerunning the agent. The summary
reports event counts, tools/read paths, context states/sources, compaction
sizes, scheduled route operations and route roles, and audit event counts.

Second slice shipped: `answer_span_evidence` links each committed answer span
to overlapping trace evidence from read/verified context ledger entries and
scheduled graph-route locations. The compact summary reports
`trace_evidenced_count` separately from `read_confirmed_count`, so graph-provided
anchors and file-read-confirmed anchors can be audited independently.

Third slice shipped: the feedback aggregate consumes `answer_span_evidence`
or derives it from old cell `answer_spans + trace`, then reports answer span
count (`ans`), trace evidence coverage (`te%`), read-confirmed coverage
(`rc%`), and unconfirmed span count (`unc`) per arm/category.

Third gate slice shipped: answer evidence is now promotable, not only
descriptive. The feedback aggregate carries worst-case evidence fields
(`te_min%`, `rc_min%`, `unc_max`) alongside means, and
`aggregate_feedback_probe.py --require-answer-evidence <arm>` writes
`answer_evidence_gate.json` then exits non-zero if a promoted arm has no parsed
answer spans, any committed span lacks trace evidence, or an optional
read-confirmed threshold is missed. This prevents a pooled recall win from
hiding one unauditable final answer.

Fourth gate slice shipped: promotion profiles make milestone exits stable.
`aggregate_feedback_probe.py --promotion-profile haiku_compact` expands to the
feedback iteration gate for `preinj_eager_compact` with savings required plus
the answer evidence and exact replay-readiness gates, writes
`promotion_profile_gate.json`, and still emits the underlying gate artifacts.
This gives autonomous iterations one named contract instead of a hand-built CLI
bundle that can drift between runs.

Fifth gate slice shipped: exact replay is now an aggregate promotion condition.
`aggregate_feedback_probe.py --require-exact-replay <arm>` writes
`replay_readiness_gate.json` and fails when any promoted cell lacks event replay
data, a full message transcript, complete tool result/envelope content, or
LLM-call message/tool-schema indexes required by `replay_readiness`. The
`haiku_compact` and `route_lifecycle` profiles include this gate, so strong
local recall cannot be promoted from artifacts that cannot be replayed exactly.

Sixth gate slice shipped: profile output now includes a single autonomous
handoff decision. When a promotion profile is requested, the aggregate writes
`promotion_decision.json` and renders a Promotion Decision table with
`promote` or `revise`, the primary action, concrete next actions, and the
profile blockers. Automation can consume this one file while humans still get
the underlying gate artifacts for diagnosis.

Seventh gate slice shipped: the handoff is now executable.
`scripts/agent_compile/run_feedback_iteration.py` runs the profile aggregate
over an output directory's `cells/`, captures the verbose aggregate report into
`feedback_iteration_aggregate.stdout` / `.stderr`, prints
`promotion_decision.json` as the machine-readable result, and exits `0` for
`promote`, `2` for `revise`, or `3` when a requested existing decision is
missing. It can also read an existing decision file directly, which gives CI or
automation a stable boundary without parsing markdown.

Eighth gate slice shipped: revise decisions now route to concrete follow-up
work. `run_feedback_iteration.py` writes `feedback_iteration_handoff.json`,
which preserves the promotion decision and expands `next_actions` into ordered
steps with reasons, relevant artifacts, and suggested commands. The first step
is the next iteration's default repair target, so automation can distinguish
replay artifact gaps, answer evidence gaps, recall regressions, missing paired
baselines, cost-only regressions, route lifecycle gaps, and typed tool-envelope
gaps without reparsing the full report.

Ninth gate slice shipped: the handoff now exposes a concrete loop edge.
`feedback_iteration_handoff.json` includes `primary_step`, `next_command`,
per-step `target_arm`, and `resolved_commands` rendered from the current
`cells/`, output directory, promotion profile, and inferred arm. Unresolved
placeholders are listed explicitly, and `next_command_ready` is true only when
the primary command has no unresolved inputs. Automation can run the next
command when the handoff is complete or stop on a precise missing input instead
of guessing from prose.

Tenth gate slice shipped: the loop edge is now executable through a restricted
runner. `run_feedback_iteration.py --execute-handoff <handoff>` reads
`feedback_iteration_handoff.json`, refuses not-ready commands, rejects commands
outside the allowlisted agent-compile diagnostic scripts, runs approved commands
without a shell, and writes `feedback_iteration_followup.json` plus captured
stdout/stderr files. This gives CI or a supervising agent a concrete
read-decision -> run-next-diagnostic -> inspect-followup boundary instead of a
free-form instruction.

Eleventh gate slice shipped: follow-up inspection is now machine-readable.
`feedback_iteration_followup.json` includes an `assessment` that classifies the
follow-up as `complete`, `continue`, `recheck`, `needs_repair`, `blocked`, or
`unknown`, maps failed diagnostics back to the repair lane, and points at the
stdout/stderr or handoff artifacts to inspect. This closes the first
decision -> handoff -> execute -> assess loop without requiring another agent
to parse command output free-form.

Twelfth gate slice shipped: assessments now produce next-plan artifacts.
`run_feedback_iteration.py --plan-from-followup <followup>` writes
`feedback_iteration_next_plan.json`. The first concrete repair lane is
`tune_compact_policy`: it reads `compact_policy_plan.json`, selects the target
arm's `preload_patch`, materializes `feedback_iteration_derived_config.yaml`
with a full preload mapping so shallow config inheritance does not drop sibling
arms, and emits the next Haiku probe and promotion-gate commands with the real
config path. Other lanes still produce conservative manual/blocked/recheck
plans with explicit artifacts to inspect, so the loop can advance without
reparsing stdout or inventing unsafe config edits.

Thirteenth gate slice shipped: next-plan commands are executable through the
same restricted boundary. `run_feedback_iteration.py --execute-next-plan
<next-plan>` runs one indexed `suggested_commands` entry without a shell, rejects
non-allowlisted scripts, captures stdout/stderr, and writes
`feedback_iteration_next_plan_execution.json`. The execution assessment tells
automation whether to run the next indexed command, inspect failed outputs, or
fix the next-plan command contract.

Fourteenth gate slice shipped: next-plan continuation is now an auditable
series. `run_feedback_iteration.py --execute-next-plan-all <next-plan>` runs
the materialized `suggested_commands` sequentially through the same allowlist,
writes per-command stdout/stderr files, stops on the first not-ready, rejected,
or failing command, and records
`feedback_iteration_next_plan_execution_series.json` with completed count,
failed command index, and a machine-readable assessment. This fixes the
current continuation boundary: a supervising agent can now see exactly how far
the generated plan advanced and which command/artifact should drive the next
repair iteration.

Fifteenth gate slice shipped: answer-quality failures now enter the same
next-plan loop instead of falling back to markdown inspection. When a follow-up
assessment asks for `restore_answer_recall` or `repair_answer_evidence`,
`run_feedback_iteration.py --plan-from-followup <followup>` reads
`answer_diagnosis_plan.json`, selects the target arm, records a
`repair_contract` for rank ordering, path normalization, schema repair, context
routing, or evidence provenance, and emits the narrow aggregate gate plus the
promotion-profile recheck as materialized `suggested_commands`. Missing
diagnosis artifacts block with a concrete `missing_artifacts` list. This does
not pretend to patch the runner automatically; it makes the required code
repair and its verification gate explicit enough for the supervising agent to
iterate without reparsing the aggregate report.

Sixteenth gate slice shipped: completed next-plan series now lift promotion
decisions into the continuation artifact. When an allowlisted suggested command
prints a `promotion_decision.json` object, the series step records that
decision and the inferred or explicit `feedback_iteration_handoff.json` path.
The series assessment now returns `record_promoted_slice` for promoted runs and
`execute_handoff` with a ready `--execute-handoff` command for revised runs.
This closes another supervision gap: after a sweep and promotion recheck, the
outer agent no longer has to manually parse stdout to decide whether to stop or
run the next diagnostic handoff.

Seventeenth gate slice shipped: external-agent baselines now have a feedback
loop attachment point. `aggregate_external_agent_baseline.py` writes
`external_agent_command.json` next to the baseline report and gate, preserving
the GT cell, run labels, gate criteria, and a shell-quoted rerun command.
`run_feedback_iteration.py --plan-from-followup <followup>` recognizes
`compare_external_agent_baseline`, reads `external_agent_gate.json` plus the
baseline/command artifacts, and emits an `external_baseline_contract` that maps
run blockers to concrete repair surfaces: MCP policy, MCP transport noise,
missing graph-MCP adoption, missing native IDE/LSP surface, path/schema/order
answer gaps, or true external context-routing misses. A green external gate
completes the plan; a red gate with a command manifest becomes a ready
next-plan command; a missing gate blocks with `missing_artifacts`. This makes
Claude/Codex/OpenCode comparisons part of the same auditable iteration loop as
CodeMiner's Haiku feedback probe.

Eighteenth gate slice shipped: external comparisons now have a same-task gap
matrix. `aggregate_external_agent_baseline.py --codeminer-cell <label=cell>`
loads one or more CodeMiner synthesis cells as reference rows and writes
`external_agent_comparison_matrix.json` / `.md`. Each external run is compared
against the selected CodeMiner reference on strict `rec@5`, parsed `rec@all`,
answer diagnosis, tool count, turns, CodeMiner `lsp_route` tool use, scheduled
route timing, native-LSP availability, MCP cancellations, and stderr transport
noise. The matrix classifies the dominant gap as `answer_contract_gap`,
`tool_adoption_gap`, `route_timing_gap`, `mcp_policy_gap`,
`mcp_transport_noise`, `native_lsp_surface_missing`,
`context_routing_gap`, or `competitive`. The feedback next-plan contract now
includes matrix gap counts when the artifact is present, so a red external run
can be attributed to a concrete comparison failure mode without manually
reading JSONL traces or aggregate tables.

Nineteenth gate slice shipped: external comparison gaps now produce repair
plans. `aggregate_external_agent_baseline.py` writes
`external_agent_repair_plan.json` / `.md` from the comparison matrix and gate
blockers. Each action records the run label, gap category, repair action,
repair surface, suggested prompt/policy/environment delta, supporting evidence,
and rerun command. The first contract maps answer-contract gaps to stricter
repo-relative final-answer prompts, tool-adoption gaps to earlier or forced
`lsp_route`, route-timing gaps to earlier route/finalization behavior, MCP
policy gaps to reruns where MCP calls can complete, MCP transport noise to
stderr classification, native-LSP gaps to IDE/LSP environment setup, and
context-routing gaps to route-seed or graph-MCP guidance. The feedback
next-plan contract now carries the repair plan actions when present, so a
supervising agent can choose the next external-run repair without reparsing
JSONL or manually translating gap labels.

Twentieth gate slice shipped: external repair actions now materialize
launcher-neutral rerun specs. The baseline aggregate writes
`external_agent_rerun_specs.json` / `.md` with one spec per repair action:
launcher status, required policy or environment, prompt delta, expected JSONL
and stderr output paths, validation command, and supporting comparison
evidence. Prompt/schema, tool-adoption, route-timing, and context-routing gaps
are marked ready for a wrapper to execute; MCP policy gaps require an explicit
allow-MCP policy; native-LSP gaps require an IDE/LSP-attached environment; MCP
transport noise requires JSONL plus stderr capture. The feedback next-plan
contract includes these specs when present, which gives a future Claude,
Codex, or OpenCode launcher a stable input contract without assuming any
particular binary is installed in the current environment.

Twenty-first gate slice shipped: rerun specs now have an auditable executor
boundary. `scripts/agent_compile/run_external_agent_rerun.py` reads
`external_agent_rerun_specs.json`, selects a spec, writes a prompt file with
the required delta plus the strict final-answer contract, and records
`external_agent_rerun_execution.<index>.json`. By default it is a dry run, so
the feedback loop can materialize launcher inputs without requiring Claude,
Codex, or OpenCode to be installed. With `--execute --command-template`, it
runs an external wrapper without a shell, captures stdout to the spec JSONL
path and stderr to the spec stderr path, and records argv, duration, and exit
status. Specs marked `needs_policy` or `needs_environment` block execution
unless the matching `--allow-requirement` is supplied. The feedback next-plan
now suggests this dry-run materialization before rerunning the external
baseline gate, closing the handoff from comparison repair plan to concrete
launcher input artifacts.

Twenty-second gate slice shipped: external rerun specs now support explicit
provider profiles. `run_external_agent_rerun.py --provider-profile` can
materialize launcher contracts for `codex_readonly`, `codex_mcp_bypass`,
`claude_code_ide`, `claude_code_bare`, and `opencode`. A profile supplies the
default command template when no explicit `--command-template` is provided,
writes `external_agent_provider_profile.<index>.json`, and records provider
requirements alongside spec requirements. Dry runs still never execute or
block on missing policy/environment allowances; they only record
`requirements_allowed` and `ready_to_execute`. Actual execution blocks until
every required allowance is explicitly supplied. These profiles are launcher
contracts, not proof that the local binary or IDE integration is valid; a run
must still be validated from its JSONL/stderr trace before promotion.

Fourth slice shipped: `summarize_trace.py --format replay-json` and
`--format replay-markdown` build a deterministic replay timeline from persisted
cell traces, including turn-ordered assistant/tool/compaction/scheduled-context
events, context ledger, final answer spans, and answer evidence.

Fifth slice shipped: answer failure diagnosis is now shared by external-agent
baselines, base sweeps, and synthesis cells. Cells record `answer_diagnosis`,
`answer_top_spans`, `answer_all_metrics`, and `answer_path_alias_metrics`; the
main aggregate and feedback probe both report a compact `diag` mix so a low
`rec@5` can be separated into `content_gap`, `format_gap`, `path_alias_gap`, or
`rank_gap` without relaxing the strict `Locations:` score.

Sixth slice shipped: the feedback probe now turns that diagnosis mix into an
`answer_diagnosis_plan` artifact. The markdown report includes an Answer
Diagnosis Plan table, and `--output-dir` writes `answer_diagnosis_plan.json`
next to the compact policy plan. Dominant `rank_gap`, `path_alias_gap`,
`format_gap`/`empty_answer`, and `content_gap` map to concrete next actions:
tighten answer ordering, normalize repo-relative paths, strengthen schema
repair, or improve context routing.

Seventh slice shipped: the first diagnosis-driven repair is implemented for
`path_alias_gap`. `run_cell` now performs a deterministic final-answer path
normalization pass before scoring: if a `Files:`, `Symbols:`, or `Locations:`
contract path is a basename or suffix that resolves uniquely from read/preload/
retrieval evidence or the repo tree, it is rewritten to the repo-relative path.
Ambiguous basenames are left untouched. The repair emits an
`answer_path_alias_normalization` trace event and cell-level
`answer_path_alias_normalized` / `answer_path_alias_replacements` fields, and
the feedback probe reports `alias_norm%` plus `alias_rep`.

Eighth slice shipped: `rank_gap` now has a general deterministic repair path,
not only the scheduled-route special case. `run_cell` performs an evidence-aware
`Locations:` ordering pass after path normalization. It never invents new
locations and never reads GT; it only promotes existing answer spans into the
scored top-k when they overlap read-confirmed or scheduled graph/LSP trace
evidence. The repair emits an `answer_location_order_normalization` trace event,
persists `answer_location_order_normalized` /
`answer_location_order_promotions`, and the feedback probe reports
`ord_norm%` plus `ord_prom`.

Exit criteria:

- a localization cell can be replayed from trace data;
- final scored spans can be linked back to read-confirmed ledger entries;
- aggregate scripts can stratify by context source and consumption state.

### P2: Compaction V2

Replace eager history reset with a general compaction pass that keeps protected
system/task constraints, preserves a small recent tail, summarizes old
exploration, and truncates stale tool output. Weak-model variants may keep one
recent read inline, but the choice should be encoded as a router decision.

First slice shipped: eager compaction now builds a deterministic exploration
summary from the context ledger, includes confirmed reads / offered context /
rejected duplicate context in the compacted seed, and records a
`compaction/summarized` ledger entry for feedback aggregation. The feedback
report also tracks pruned tool-output characters, compact seed size, and
protected tail size so M2 changes are measurable rather than just prompt prose.

Second slice shipped: the 27-cell Haiku feedback probe passed the M2 gate with
no category-level recall regression. The useful remaining M2 work is tuning
seed/tail size from trace rows, not adding more prompt prose.

Third slice shipped: feedback aggregation now turns compact trace metadata into
an explicit policy hint. Reports include compact prune ratio (`cprune%`), tail
share (`tail%`), kept read-output count (`keep`), and `cpol` values such as
`hold`, `try_keep1`, `trim_tail`, `weak_prune`, or `rec_regress`. This makes
M2.3 iterative: repeated-read tax points to keeping one read output, excessive
protected tail points to trimming the tail, and a green save with stable recall
points to holding the current policy.

Fourth slice shipped: the `trim_tail` hint is now executable. `AgentRunner`
accepts `compact_tail_chars`, the compact summary records both requested and
actual protected-tail size, and `run_cell` forwards
`preload.<arm>.compact_tail_chars` for `eager_compact` arms. The default remains
800 chars, so old configs keep their behavior; new M2.3 probes can test a
smaller tail without editing runner code.

Fifth slice shipped: feedback aggregation now emits an explicit compact policy
plan. The markdown report has a `Compact Policy Plan` table, the JSON aggregate
contains `compact_policy_plan`, and `compact_policy_plan.json` is written next
to `feedback_probe.json`. `try_keep1` plans patch `compact_keep_reads: 1`,
`trim_tail` plans patch a smaller `compact_tail_chars`, and reject/hold states
carry no speculative patch.

Exit criteria:

- compact mode remains iso-accuracy to grep on base and synthesis sweeps;
- token savings are significant on paired bootstrap;
- compaction summaries list accepted anchors and rejected dead ends.

### P3: Preload-Aware Routing

Turn pre-load from a static prompt variant into a state machine: verify top
candidates, mark false positives, fall back to grep when context is thin, and
invoke graph expansion only for tasks where edge-following is expected to
substitute for search fan-out.

First slice shipped: preload candidates are durable context ledger entries.
Each candidate records rank and span; reads mark overlapping candidates as
verified, and compaction summaries plus feedback reports expose preload offered
count and verification rate. A follow-up smoke showed the same symbol-hint
query at `rec@5=1.000` with span-aware `pre_vfy%` of 40% for compact mode,
which is actionable: some candidates were useful, but many were unconfirmed.

Second slice shipped: unverified preload no longer triggers eager compaction.
If reads fail to overlap any preloaded span, the runner records `fallback`,
adds an explicit grep/read fallback nudge, and waits for a verified read before
collapsing the context.

Exit criteria:

- weak models use keep-read variants when they need them;
- behavioral or no-hint queries are not regressed by forced pre-load;
- graph context is measured as a deterministic harness action, not only an
  optional tool call.

Latest answer-diagnosis slice: recent Haiku route probes show format failure is
not the dominant issue. In the last 80 local Haiku result directories, only
1/357 successful cells had a format gap under the current parser; the notable
Caddy bridge miss was a `rank_gap`, where the answer named the correct provider
symbol but did not put its exact range into `Locations` top-5. Feedback
aggregation now backfills answer diagnoses from persisted spans/GT blocks, and
the harness has a deterministic named-anchor patch: if a read-confirmed
scheduled anchor is explicitly named in `Symbols:` but missing from `Locations`
top-k, its precise scheduled range is promoted into the compact `Locations`
line without spending another LLM turn. The actual Caddy miss replay patches
`replacer.go:297-337` into the first five locations.

Follow-up diagnosis slice: the runner now records deterministic answer repair
actions separately from answer quality. Path alias normalization resolves
unique basename/suffix answers to repo-relative paths, location-order
normalization promotes read-confirmed or scheduled evidence into top-k when the
model already listed it, and schema salvage repairs the narrow `format_gap`
case where the answer has explicit `Files:`/`Symbols:` but no `Locations:`.
Schema salvage uses only read-confirmed trace spans or actual `read`
offset/limit windows for the explicitly named files; it does not scrape prose or
relax the scorer. Feedback reports include `alias_norm%`, `ord_norm%`, and
`schema_salv%`, so future small Haiku probes can distinguish "answer was
accurate but hard to parse/order" from true `content_gap` routing failures.

### P4: Edit-Capable Harness Envelope

Before using the runner for edit tasks, add snapshot, patch metadata, revert,
permission boundaries, targeted tests, and diagnostics feedback. This phase
should start only after P1 traces can explain localization decisions.

First slice shipped: `scripts.agent_compile.lib.edit_audit` wraps an arbitrary
edit attempt with before/after git status and binary diffs, preimage snapshots
for files that were already dirty, a generated `revert.py`/manifest that
restores the worktree to the edit-run starting state, and an optional smallest
verification command record. The dirty-start case is covered so a revert does
not discard user changes that existed before the agent edited.

Second slice shipped: `run_edit_command_with_audit` and
`scripts/agent_compile/run_edit_audit.py` provide a concrete audited runner
surface for any edit command. The audit now records the edit command itself
(command, cwd, exit, duration, stdout/stderr, timeout/truncation) before
finalizing diff/revert and optional verification artifacts. This can wrap an
external edit agent today and is the attachment point for a future LLM-native
edit tool.

Third slice shipped: `run_agent_with_edit_audit` wraps an `AgentRunner.run()`
edit attempt directly. It starts the same before-state audit, lets the LLM run
with normal tools, writes `agent_result.json` with answer, turns, messages,
tool-call records, trace, usage, and typed tool envelopes, then finalizes
after-state diff/revert artifacts and optional verification. If the runner
raises after touching files, the wrapper still writes `agent_error.json`,
finalizes `edit_audit.json`, and raises `AgentEditRunAuditError` carrying the
audit object. Unit gates cover both a fake LLM invoking the default `bash` tool
to edit a git repo and a runner that edits then crashes; both verify the
generated revert script restores the starting worktree.

Exit criteria:

- every edit run has before/after diff metadata and a revert path;
- tool permissions are explicit for external paths and shell commands;
- the harness records the smallest local verification command that was run.

### P5: Typed Tool Envelope

Normalize tool outcomes so replay and aggregate code do not infer behavior from
free-form error strings. Tool calls should expose a uniform envelope across
runner records, trace events, context ledger entries, and synthesis cell
summaries.

First slice shipped: `ToolResultEnvelope` records `status`, `result_type`,
`result_count`, `result_chars`, `duration_ms`, `truncated`, `error_kind`, and
optional metadata. Runner tool records and trace events now attach the envelope;
context ledger metadata stores it as `tool_result`; synthesis `tool_calls`
summaries preserve it. The first tests cover successful calls, unavailable
tools, invalid arguments, executor exceptions, duplicate-call skips, and
truncated results.

Second slice shipped: default file and shell tools now attach explicit
permission/sandbox metadata to the same envelope. `read`, `grep`, and `glob`
record `process_read_permissions`, `caller_managed` sandboxing, `path_jail=none`,
path scope, and `side_effects_possible=false`; `bash` records
`process_exec_permissions`, `shell=true`, command size, optional `cwd`/timeout,
and `side_effects_possible=true`. Replay summaries surface the compact
`perm=... sandbox=... sidefx=...` view so this is visible without inspecting raw
JSON.

Third slice shipped: `bash` results are classified structurally. Nonzero exits
set `status=error`, `error_kind=exit_nonzero`, and `exit_code`; spawn/cwd
failures set `error_kind=spawn_error`; timeout errors set `error_kind=timeout`;
and bash output caps set `truncated=true` plus `output_truncated=true`. Replay
summaries now include `exit=N` alongside the permission view.

Fourth slice shipped: the feedback aggregate now consumes typed tool envelopes
before falling back to legacy trace fields. Reports expose tool-call count,
envelope coverage (`tenv%`), envelope-derived errors/skips/truncations
(`terr`, `tskip`, `ttrunc`), and result payload size (`tchars_k`). It also
writes `tool_envelope_plan.json` and renders a Tool Envelope Plan table, so
missing envelope coverage, typed tool errors, skipped calls, and truncation are
machine-readable follow-up actions. `lsp_route` result counts also prefer
envelope `result_count`, so route comparisons do not depend on ad hoc cell
summary fields when schema-v2 traces are available.

Fifth slice shipped: the main cost-arm aggregate consumes the same tool-call
envelopes from sweep cell `tool_calls[]`. `metrics.json` now carries per-arm
`tool_envelope` health, and `report.md` includes a Tool Envelope Health table
with envelope coverage, typed error/skip/truncation counts, and result payload
size. This makes P5 visible in both the small feedback loop and the broader
cost-arm reports.

Sixth slice shipped: both aggregates now have an opt-in tool-envelope hard
gate. `aggregate_feedback_probe.py --require-tool-envelope-health` and
`aggregate.py --require-tool-envelope-health` write their normal artifacts
first, then return non-zero unless every arm has observed tool calls, full
typed-envelope coverage, and no typed tool errors, skips, or truncation. This
keeps historical artifact aggregation permissive while giving CI and promoted
schema-v2 sweeps a direct machine-checkable exit criterion.

Exit criteria:

- invalid args, unavailable tools, executor exceptions, duplicate skips, and
  default-tool `Error:` results are distinguishable without parsing strings;
- truncation and result sizes are explicit for read/grep/bash outputs;
- permission and sandbox metadata are recorded for shell/file tools;
- replay and aggregate code consume the envelope before falling back to legacy
  fields.

## Non-Goals

- Do not add more graph or retrieval tools just because the agent ignored the
  previous ones.
- Do not treat lower retrieval recall as decisive if the agent compensates with
  grep/read.
- Do not compare local Qwen runs by dollar cost when `cost_usd` is unavailable.
- Do not promote partial or single-arm sweeps to headline conclusions.
