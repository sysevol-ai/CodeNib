<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent Runner Architecture Goal

Status: Active architecture goal
Last revised: 2026-07-04

This page is the durable target for the post-#271 agent runner work. PR #271 is
kept as a spike and evidence bundle, not as a merge candidate. The goal is to
extract the useful findings into reviewable core changes while preventing the
runner from overfitting to a narrow scorer, dataset slice, or answer format.

## Program Goal

Build an agent harness that turns CodeMiner's graph, chunking, and repository
analysis into durable agent context, not benchmark-shaped prompt tricks. The
runner should make context selection, tool use, and finalization observable and
repeatable across models and repositories. Experiments should prove which
capabilities help, but the implementation should promote only reusable runtime
or evaluation APIs.

The program is successful when:

- Core agent modules expose stable contracts for runtime state, tool calls,
  context accounting, and graph-backed navigation.
- Evaluation modules can replay and diagnose runs without depending on
  experiment scripts.
- Scripts contain configuration, dataset selection, and CLI/report glue only.
- Every promoted behavior has evidence from raw and normalized metrics across a
  fixed smoke set and at least one rotating holdout surface.
- Overfit-risk logic is isolated in evaluation normalization or experiment
  recipes, never in runtime defaults.

## North Star

CodeMiner's agent runner should use repository structure better than a plain
LLM shell by exposing graph/LSP context as first-class, provenance-rich tools.
The runtime should help an agent spend context deliberately: every injected
context item should have source, freshness, cost, and consumption state, and the
agent should be able to explain why it read, skipped, or finalized.

The implementation must keep three layers separate:

| Layer | Owns | Must not own |
| --- | --- | --- |
| Core runtime | Tool execution, trace events, context ledger, resource guards, graph/LSP tool contracts | SWE-bench scoring rules, `Locations:` top-k correction, experiment promotion gates |
| Evaluation harness | Trace replay, metrics, baselines, failure diagnosis, promotion gates | Default agent behavior or production tool scheduling |
| Scripts | Thin CLI entrypoints, experiment configs, report generation | Reusable libraries or policy logic |

## Guardrails

- No dataset-instance conditionals in core runtime. Repo or language behavior
  must be derived from capabilities, manifests, or tool results.
- No scorer-specific mutation in core runtime. Answer schema repair, top-k
  reordering, and format salvage are evaluation-layer normalizations only.
- Runtime prompts may describe output contracts, but they must not mention
  current benchmark metrics, promotion gates, or hidden scoring priorities.
- Evaluation gates must include at least one held-out or rotating surface before
  a runtime default is promoted.
- Every heuristic promoted to runtime must be explainable without naming a
  specific benchmark instance such as Caddy, Ruff, or Astropy.
- `scripts/agent_compile` may import core/eval packages, but core packages must
  not import `scripts.agent_compile`.

## Milestones

### Active Iteration Sequence

The milestone labels above describe the long-term architecture. The current
implementation sequence should stay smaller and reviewable. A milestone is only
done when it improves one of the layer boundaries above and has a test or report
that would catch regression independent of a single scorer result:

| Step | Scope | Review Boundary | Promotion Signal |
| --- | --- | --- | --- |
| M3b | Context ledger runtime contract | `codeminer/agent/runtime` only; no scorer logic | Unit tests explain provenance and consumption state |
| M4a | Declarative harness spec | Runner construction knobs only; no dataset/model policy | Scripts instantiate a core spec instead of open-coding runner kwargs |
| M4b | Shared harness plumbing | Usage accounting, repo cwd scoping, skill context loading, prebuilt graph helpers | `scripts.agent_compile.lib` shrinks while eval tests target package APIs |
| M4c | Eval diagnostics package | Trace summaries, answer diagnostics, edit audit, external-agent baselines | Scripts become CLI wrappers over `codeminer/eval/agent_runner` |
| M5a | Feedback loop surface | Small fixed smoke set plus rotating holdout, raw vs normalized metrics | Iterations report content/rank/path/format/runtime gaps separately |
| M6a | Quarantine overfit-risk logic | Scorer repair, required-anchor correction, route promotion gates | Runtime defaults can be justified without naming benchmark instances |

Do not skip from M4b to a broad "agent improvement" PR. Each PR should either
move reusable plumbing into a package, add a boundary test, or improve the
feedback signal. Experiment recipes may use model-specific heuristics, but
runtime and reusable eval APIs must not encode the current scorer, answer
format, or a tiny fixed dataset slice.

Current operating order:

1. Land the small core/runtime foundations already under review.
2. Publish one M4b PR that removes reusable harness plumbing from
   `scripts/agent_compile/lib` without changing benchmark policy.
3. Publish one M4c PR that moves diagnostics and external-run analysis into
   `codeminer/eval/agent_runner`.
4. Build the M5 feedback loop on a small smoke set plus rotating holdout before
   promoting any runtime default.
5. Delete or quarantine M6 overfit-risk logic before treating #271 as closed.

Any proposed optimization that cannot pass this order stays experiment-only.

### M0: Freeze And Audit #271

Objective: preserve #271 as a spike and classify its contents before extracting
anything.

Deliverables:

- PR title/body mark #271 as draft, spike, and do-not-merge.
- Inventory changed files into keep, move, rewrite, and drop buckets.
- Identify scorer-coupled logic and instance-specific heuristics.
- Record which local/remote tests are meaningful evidence and which are only
  spike validation.

Exit criteria:

- No one can reasonably mistake #271 for a ready feature PR.
- The next PR can be scoped without rereading the whole spike.

### M1: Define Package Boundaries

Objective: create the target module layout before moving code.

Proposed layout:

- `codeminer/agent/runtime/`: trace events, tool call records, context ledger,
  duplicate-call guard, final-answer contract helpers.
- `codeminer/agent/tools/`: graph-backed LSP route/definition/reference tool
  contracts and shared tool result schemas.
- `codeminer/eval/agent_runner/`: trace replay, answer diagnostics, external
  baseline parsing, promotion gates, edit-run audit.
- `scripts/agent_compile/`: CLI wrappers and experiment configs only.

Exit criteria:

- A short ADR or this page defines allowed import directions.
- Tests enforce that `codeminer/*` does not import `scripts.agent_compile`.

### M2: Extract Minimal Graph/LSP Route Core

Objective: ship the smallest generally useful product feature from #271.

Deliverables:

- Core graph/LSP route services and result schemas.
- MCP tools for `lsp_definition`, `lsp_references`, and `lsp_route`.
- Focused tests over small synthetic graphs and existing graph fixtures.
- No route lifecycle gates, answer rewrites, or scorer-facing feedback logic.

Exit criteria:

- A user can call graph-backed LSP tools independently of the experiment
  harness.
- Unit and MCP tests pass without `scripts/agent_compile` imports.

### M3: Extract Runtime Observability

Objective: make agent runs inspectable without embedding experiment policy in
the runner.

Deliverables:

- Stable trace event schema for tool calls, reads, errors, skips, context
  injections, and final answers.
- Context ledger API with provenance and consumption state.
- Duplicate-tool/read guard that records events but does not optimize for a
  benchmark-specific final answer.

Exit criteria:

- Trace replay can explain a run without reading provider logs.
- Existing agent tests cover the event contract and failure paths.

### M4: Rebuild Evaluation Harness As Eval Package

Objective: move reusable experiment logic out of scripts while keeping it out of
runtime.

Deliverables:

- Trace summary, answer diagnosis, feedback aggregation, external-agent
  baseline parsing, and edit audit live under `codeminer/eval/agent_runner/`.
- Scripts become thin argument parsers that call eval package APIs.
- Tests target eval APIs directly, with a small number of CLI smoke tests.

Exit criteria:

- `scripts/agent_compile/lib` is empty or removed.
- No test imports reusable logic from `scripts.agent_compile.lib`.

### M5: Generalization Gates

Objective: make feedback iteration useful without rewarding narrow metric hacks.

Deliverables:

- Frozen regression surface plus a rotating holdout surface.
- Reports separate content gaps, format gaps, rank gaps, path alias gaps, and
  runtime/tooling failures.
- Promotion profiles require evidence on more than one language and more than
  one query family.

Exit criteria:

- A runtime default cannot be promoted based only on a small fixed set of
  Caddy/Ruff/Astropy cells.
- Evaluation normalization is reported separately from raw answer behavior.

### M6: Retire Or Rewrite Scorer-Coupled Spike Logic

Objective: remove the parts of #271 that made the runner look like it was
optimizing the benchmark harness instead of helping agents reason.

Deliverables:

- Delete or quarantine answer `Locations:` reordering and required-anchor
  correction from runtime paths.
- Keep schema parsing and normalization only in eval, with before/after metrics.
- Replace instance-specific route ranking with capability-derived routing
  policies or leave it as experiment-only.

Exit criteria:

- Runtime behavior can be explained without mentioning `rec@5`, answer-block
  scoring, or a named benchmark instance.

### M7: CI And Test Tier Cleanup

Objective: keep iteration fast without hiding real failures.

Deliverables:

- Unit tier contains pure logic and mocks only.
- GPU/embedding e2e tests are marked slow and excluded from unit.
- CI dependency setup avoids accidental CUDA downloads in non-GPU jobs.
- Slow/integration failures identify infra/config issues separately from code
  regressions.

Exit criteria:

- Unit CI is a reliable feedback loop for core changes.
- Heavy jobs are still available for promotion gates but do not block every
  architecture edit.

## First Extraction Rule

The first non-spike PR should be intentionally small: graph/LSP route tools plus
their MCP surface, or runtime trace contracts, but not both if that makes review
hard. The purpose is to re-establish clean ownership before optimizing the
agent.

## Initial #271 Inventory

This is the first-pass split of the spike branch. It is intentionally coarse:
the point is to choose review boundaries before moving code.

### Keep And Extract First

These look like product-facing core capabilities if they can stand alone:

- `codeminer/agent/skills/lsp_definition/`
- `codeminer/agent/skills/lsp_references/`
- `codeminer/agent/skills/lsp_route/`
- `codeminer/mcp/tools/lsp.py`
- the minimal MCP server and docs wiring needed for those tools
- focused tests in `test/mcp/` and `test/agent/test_graph_nav.py`

Extraction rule: no dependency on `scripts/agent_compile`, no feedback gates,
no answer rewriting.

### Extract Later After Boundary Review

These are useful but need a cleaner runtime API first:

- trace/tool-result envelope changes in `codeminer/agent/runner.py`
- context-ledger and duplicate-call guard behavior
- shared types now mixed into `codeminer/agent/agent_types.py`
- runner tests that assert stable event contracts rather than exact benchmark
  trajectories

Extraction rule: runtime events should describe what happened; they should not
encode benchmark promotion logic.

### Move To Evaluation Package

These are reusable evaluation or analysis libraries, not scripts:

- `scripts/agent_compile/lib/trace_summary.py`
- `scripts/agent_compile/lib/answer_diagnostics.py`
- `scripts/agent_compile/lib/edit_audit.py`
- external-agent baseline parsing and rerun helpers
- feedback aggregation and replay-readiness reports

Target: `codeminer/eval/agent_runner/`, with scripts reduced to CLI wrappers.

### Rewrite Or Quarantine

These are the highest overfit-risk areas and should not move to runtime as-is:

- `Locations:` top-k reordering
- required-anchor correction prompts
- answer schema salvage that changes scored output
- route lifecycle promotion gates tied to a tiny fixed query surface
- instance- or project-shaped heuristics such as Caddy/Ruff/Astropy-specific
  route behavior

Allowed destination: evaluation-only normalization or experiment-only configs,
with raw and normalized metrics reported separately.

### CI/Test Tier Follow-Up

These changes are real but should be split from agent architecture:

- CPU PyTorch install setup for non-GPU jobs
- marker cleanup for GPU embedding e2e tests
- documentation of unit/slow boundaries

Target: a small CI PR, because it improves iteration independent of runner
architecture.
