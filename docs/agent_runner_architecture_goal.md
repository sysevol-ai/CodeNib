<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent Runner Architecture Goal

Status: Active architecture program
Last revised: 2026-07-05

This page is the durable plan for the post-#271 agent runner work. PR #271 stays
as a spike and evidence bundle, not as a merge candidate. The useful pieces must
be extracted through small reviewable PRs that improve CodeMiner's reusable
runner, graph tooling, or evaluation harness without baking benchmark-specific
behavior into production defaults.

## Operating Thesis

CodeMiner should give agents a repository-context advantage over a plain LLM
shell. The core harness should expose graph, chunk, symbol, search, and LSP
context as first-class tools with provenance, cost, freshness, and consumption
state. Experiments should measure which capabilities help, but only reusable
runtime or evaluation contracts should be promoted.

The architecture work is successful when:

- Core agent modules expose stable contracts for runtime state, tool calls,
  context accounting, graph-backed navigation, and final-answer handling.
- Evaluation modules can replay, compare, and diagnose runs without depending on
  experiment scripts.
- `scripts/agent_compile` contains CLI, configuration, dataset selection, and
  report glue only.
- Feedback loops report raw and normalized behavior across a fixed smoke set
  and a rotating holdout surface.
- Runtime defaults can be justified without naming a benchmark instance, scorer
  field, or current promotion threshold.

## Non-Goals

These are explicitly out of scope for core runtime promotion:

- Dataset-instance conditionals such as behavior shaped around Caddy, Ruff,
  Astropy, or any other named benchmark case.
- Scorer-specific answer mutation in runtime paths, including `Locations:`
  reordering, required-anchor correction, or format salvage that changes the
  scored output.
- Model-specific prompt tricks promoted as core behavior before they pass a
  cross-model or external-agent comparison.
- Moving experiment policy from `scripts/agent_compile` into `codeminer/agent`.
  Experiment policy belongs in `codeminer/eval/agent_runner` or stays in the
  experiment configuration.

## Layer Ownership

| Layer | Owns | Must not own |
| --- | --- | --- |
| Core runtime | Tool execution, trace events, context ledger, resource guards, graph/LSP tool contracts, final-answer contract primitives | SWE-bench scoring rules, `Locations:` top-k correction, dataset/model policy, promotion gates |
| Evaluation harness | Trace replay, metrics, baselines, failure diagnosis, normalization, promotion gates | Default runtime behavior or production tool scheduling |
| Scripts | CLI entrypoints, YAML configs, dataset slices, report generation | Reusable libraries, scorer repair logic, runner policy |

Allowed import direction:

```text
scripts/agent_compile -> codeminer.eval.agent_runner -> codeminer.agent
```

`codeminer/*` must not import `scripts.agent_compile`.

## Feedback Contract

Fast iteration needs a feedback loop, but the loop must not reward narrow scorer
overfitting.

- Every agent-runner iteration should have a deterministic smoke slice and a
  seed-rotated holdout slice.
- Reports must keep raw answer behavior separate from evaluation normalization.
- Failure categories should be reported separately: content gap, rank gap, path
  alias gap, format gap, context/runtime failure, and infrastructure failure.
- A runtime default cannot be promoted from a single fixed slice or a single
  model. A model-specific recipe may remain experiment-only.
- External baselines should compare CodeMiner's harness against general coding
  agents with similar task inputs, not against hidden scorer details.

## Milestone Backlog

Each milestone is intentionally larger than a single patch but should be landed
through small PRs. A milestone is done only when it has tests or reports that
would catch regressions independent of a single scorer result.

| Milestone | State | Scope | Exit Signal |
| --- | --- | --- | --- |
| M0: CI fast-feedback ordering | Landed in #284 | Slow CI waits for fast lanes; cancelled checks are treated as incomplete | Fast lanes fail before expensive slow jobs start |
| M1: Core/eval/script boundary | Landed before this plan and guarded by tests | Import boundaries and `codeminer.eval.agent_runner` package skeleton | Boundary tests fail if reusable packages import scripts |
| M2: Sweep harness package extraction | Landed in #285 | Sweep config, cell execution, prebuilt staging, preload helpers move out of scripts | Scripts call package APIs; config defaults are not hard-coded model lore |
| M3: Graph/LSP line-boundary contract | Landed in #286 | LSP-facing routes share agent line-boundary conversion | MCP and agent tests cover the shared contract |
| M4: Deterministic feedback slices | Landed in #287 | Smoke plus seed-rotated holdout planning | A small run can be selected without hand-picking project names |
| M5: Shared localization scoring | Landed in #288 | Answer/retrieval/preload scoring glue lives in eval package | Sweep scripts no longer duplicate scoring logic |
| M6: Quarantine scorer-shaped runtime defaults | Landed in #289 | Localization schema forcing becomes opt-in harness policy | Production/default runners are not shaped by localization scorer formatting |
| M7: Context ledger runtime contract | Landed in #291 | First-class context items with provenance, token/cost accounting, freshness, and consumption state | Unit tests can explain why context was injected, used, skipped, or expired |
| M8: Trace replay and diagnostics | Landed in #292 | Eval package reconstructs tool use, context consumption, answer spans, and failure category from trace records | Reports diagnose behavior without provider logs or script-specific parsing |
| M9: External-agent and LSP baseline harness | In progress | Comparable task runner for CodeMiner, Codex, Claude Code, and OpenCode-style LSP workflows | Baseline reports separate harness advantage from answer-format compliance |
| M10: Promotion gates and cleanup | Planned | Raw/normalized metrics, held-out gates, and deprecation/removal of legacy script shims | Runtime defaults require smoke plus holdout evidence; scripts stay thin |

## Immediate PR Queue

The next work should be ordered by architecture leverage, not by whichever
benchmark cell is currently easiest to improve.

1. **M7a: Context ledger primitives.**
   Add a small `codeminer/agent/runtime` contract for context entries,
   provenance, token/cost estimates, freshness, and consumption status. This PR
   should not change model prompts or scorer behavior.

2. **M8a: Trace replay summary.**
   Move run-diagnosis logic into `codeminer.eval.agent_runner` so reports can
   explain tool calls, skipped reads, repeated searches, injected context, and
   final answer spans from structured records.

3. **M9a: Baseline task adapter.**
   Landed in #293. External agents and LSP workflows now share a task/result
   envelope and JSONL record builder.

4. **M9b: Graph-backed LSP route baseline.**
   Landed in #294. Static graph LSP-route runs now emit the shared baseline
   result envelope without adding scorer repairs or external-agent-specific
   scripts.

5. **M9c: Generic baseline batch runner.**
   Landed in #295. Reusable task iteration, resume, exception capture, JSONL
   writing, and aggregate metric accounting now live in
   `codeminer.eval.agent_runner`.

6. **M9d: External loc baseline driver migration.**
   Landed in #296. The existing Codex/Claude localization baseline driver now
   uses the shared batch runner while keeping dataset selection and CLI flags in
   the driver layer.

7. **M10a: Legacy script shim cleanup.**
   Landed in #297. Internal scripts and reusable tests import package APIs
   directly. `scripts/agent_compile/lib` became temporary deprecated
   compatibility for old notebooks until the removal milestone.

8. **M10b: External LOC baseline ownership.**
   Landed in #298. Reusable external localization baseline helpers live in
   `codeminer.eval.agent_runner.loc_baseline`; maintained examples import that
   package API, and `codeminer.eval.loc_agent_runner` is a deprecated
   compatibility wrapper only.

9. **M10c: Promotion evidence contract.**
   Landed in #299. Runtime promotion evidence now has a typed evaluation
   contract so future runner defaults require raw-behavior evidence, smoke and
   holdout slices, failure category attribution, and explicit scorer/benchmark
   dependency checks before they can leave experiment policy.

10. **M10d: Legacy shim implementation guard.**
    Landed in #300. During the migration window, `scripts/agent_compile/lib`
    was guarded as import-only so compatibility modules could not regain
    functions, classes, or non-package imports that would move reusable
    ownership back into scripts.

11. **M9e: LSP route agent-tool contract guard.**
    Lock the agent-facing `lsp_route` skill contract so it remains a static
    graph-backed tool with symbol-graph requirements, array symbol inputs, and
    route anchors produced by the shared core helper rather than eval-only
    baseline code.

12. **M10e: Legacy shim namespace removal.**
    Remove `scripts/agent_compile/lib` after the migration window so experiment
    scripts no longer expose a core-looking library namespace. Reusable
    agent-runner code must be imported from `codeminer.eval.agent_runner`.

13. **M10f: Sweep execution ownership.**
    Move reusable sweep execution semantics — harness validation, resume,
    prebuilt index loading, per-cell scoring, JSON record construction, and
    transient failure persistence — into `codeminer.eval.agent_runner.sweep`.
    `scripts/agent_compile/run_sweep.py` should remain CLI/config override glue.

14. **M10g: Per-query sweep execution ownership.**
    Move reusable many-queries-per-repo execution semantics into
    `codeminer.eval.agent_runner.query_sweep`. Dataset-specific scripts may
    load and normalize rows, but context reuse, per-query scoring, resume, and
    transient failure persistence belong to the package layer.

15. **M9f: Opt-in initial static LSP route context.**
    Landed in #304. Runner/harness can extract symbol-like seeds from the task,
    route them through the graph-backed `lsp_route` skill, and inject compact
    unverified route hints into the opening prompt. The context is traced as
    harness-provided startup context, not as a model tool call, and remains
    opt-in until promotion evidence shows raw behavior improvement.

16. **M9g: Route-context seed specificity policy.**
    Add a core seed policy for opt-in route context so experiments can compare
    all extracted seeds against a specific-symbol gate without hard-coding
    instance names or scorer behavior. Generic lowercase seeds such as
    backticked common words should not trigger graph route startup context under
    the specific policy; qualified, CamelCase, all-caps, or mixed alpha-digit
    symbols may still route.

## Current Boundary Decisions

- Localization answer-contract forcing is an opt-in harness policy, not a core
  runtime default. `AgentRunner` and `AgentHarnessSpec` default to no forced
  schema turn; `SweepConfig` opts in explicitly so localization evaluations stay
  comparable while non-benchmark agent use is not shaped by scorer formatting.
- Sweep cell scoring lives in `codeminer.eval.agent_runner.scoring`. Scripts may
  write the resulting fields to JSON, but they should not reimplement format
  failure, span metrics, or preload contribution logic.
- Small feedback slices are selected by deterministic smoke plus rotated
  holdout plans, not by hand-picking named project instances.
- Small-run feedback summaries live in
  `codeminer.eval.agent_runner.feedback_summary`. Scripts may render or persist
  them, but arm grouping, baseline deltas, context-source counts, and runtime
  failure grouping should remain package APIs.
- The old `scripts/agent_compile/lib` compatibility namespace has been removed.
  New code must import reusable helpers from `codeminer.eval.agent_runner`.
- External localization baseline helpers live under
  `codeminer.eval.agent_runner.loc_baseline`; the old
  `codeminer.eval.loc_agent_runner` module is deprecated compatibility only.
- Promotion evidence records live under `codeminer.eval.agent_runner` and must
  keep scorer dependencies and named benchmark dependencies explicit. Runtime
  defaults should not be promoted when either dependency set is non-empty.
- `scripts/agent_compile` owns experiment CLIs, configs, and report glue only;
  it must not grow a reusable `lib` namespace again.
- Sweep execution semantics live in `codeminer.eval.agent_runner.sweep`;
  experiment scripts may parse arguments and select configs, but should not own
  reusable cell lifecycle, scoring, or resume behavior.
- Per-query sweep execution semantics live in
  `codeminer.eval.agent_runner.query_sweep`; experiment scripts may select a
  dataset/config but should not own query cell lifecycle, scoring, or context
  reuse behavior.
- External-agent and LSP baseline task/result envelopes live in
  `codeminer.eval.agent_runner`. Client wrappers and examples may adapt their
  SDK-specific inputs to that envelope, but they should not own reusable scoring
  or JSONL record construction.
- Static graph LSP baselines should route from explicit symbol seeds or
  code-like identifiers supplied by the task context. They must not read ground
  truth targets, mutate answer order for a scorer, or hide graph-display gaps
  with benchmark-specific normalization.
- Initial `lsp_route` prompt context is a core opt-in harness feature. Seed
  extraction and route rendering live under `codeminer.agent`; eval baselines
  may reuse them, but benchmark-specific context fields stay in eval adapters.
- Route-context seed gating is harness policy. The default stays `all` for
  compatibility, while `specific` gives sweeps a cheap feedback path for
  suppressing low-information seeds before paying the route-context token cost.
  Runner trace/ledger records this as startup context so it remains distinct
  from tools selected by the model itself.

## Promotion Checklist

Before promoting a behavior from experiment to runtime default, answer these
questions in the PR body:

- What reusable runtime or tool contract changed?
- What raw behavior improved before evaluation normalization?
- Which smoke and holdout slices were used?
- Which models or external agents were compared, if any?
- What failure category is reduced: content, rank, path alias, format,
  context/runtime, or infrastructure?
- Why is the behavior not tied to a named dataset instance or scorer field?

If those questions cannot be answered, the behavior stays experiment-only.

## Archived Spike Inventory

The #271 spike initially mixed product features, runtime observability,
evaluation diagnostics, and overfit-risk logic. The extraction rule remains:

- Product-facing graph/LSP tools can move to core only when they stand alone
  from experiment harness code.
- Runtime events should describe what happened, not encode benchmark promotion
  logic.
- Evaluation-only normalization belongs under `codeminer/eval/agent_runner`.
- Answer reordering, required-anchor correction, and route lifecycle gates tied
  to a tiny fixed query surface must be deleted, quarantined, or rewritten as
  explicit experiment policy.
