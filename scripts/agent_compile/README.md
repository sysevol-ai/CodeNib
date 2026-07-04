<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# agent_compile — design-space cost study

**Question:** given a code-localization agent, which *tool harness* matches the
accuracy of a grep/read agent at the lowest token cost? And the sharp version:
does structured retrieval + call-graph navigation *substitute* for the grep/read
fan-out, or is it additive overhead?

The experiment is **one config, many arms** — every arm shares the same model,
dataset, reps, and instance set, differing only in the tool harness, so all
arms are directly co-plottable on a single files@k-vs-tokens Pareto plot.

> **Heads-up — "CI" means *confidence interval*** here and in
> `docs/experiments/agent_compile.md` (the 95 % band on a mean token delta),
> **not** continuous integration.

## The experiment space (`configs/design_space.yaml`)

One canonical config. Each arm isolates **one axis** of the (post-prune) skill
set so its marginal effect is attributable; all arms run on the **full**
codeminer-base test split with the runner's neutral default prompt (no arm is
steered toward its skills — adoption is the model's choice):

| arm | skills (on top of the always-on `read`/`grep`/`glob`/`bash`) |
|---|---|
| `defaults_only` | — (baseline: file tools only) |
| `bm25` | bm25_search |
| `embedding` | embedding_search |
| `bm25_embedding` | bm25_search + embedding_search |
| `hybrid` | + hybrid_search fusion |
| `rerank` | embedding_search + llm_rerank |
| `graphnav` | bm25_search + find_callers/find_callees/trace |
| `composer` | codeminer_context (one-call GraphRAG) |
| `everything` | the full kept skill set at once |

`defaults_only` names the four default **tool** ids so the allowlist is
non-empty (an empty subset trips AgentRunner's empty→full-registry fallback);
they aren't skills, so they expose exactly the always-on tools. Toggle the
composer's graph expansion off with `CODEMINER_COMPOSER_NO_GRAPH=1` for an
ablation.

## Scripts

| file | purpose |
|---|---|
| `run_sweep.py` | run the sweep: `{arms} × {instances} × {reps}` agent cells on prebuilt indexes; `cells/<id>.json` + `sweep_summary.json`. Validates the harness (raises on unknown tool/skill ids) before spending. |
| `aggregate.py` | fold cells into a report: per-arm metrics, skill-invocation histogram, easy/hard split, per-scenario cells, Pareto front → `report.md` + `metrics.json`. |
| `plan_feedback_suite.py` | validate a frozen feedback-suite manifest and emit preflight, sweep, and aggregate commands plus `feedback_suite_plan.json`; this is the small-gate path for runner milestones. |
| `lib/config.py` | `SweepConfig` (base + per-arm overlay; **empty `instances` = the full split**). |
| `lib/harness.py` | dataset loading, prebuilt-index staging into agent `contexts`, scenario classification, the one `run_cell` agent call. |
| `lib/prebuilt.py` | stage offline-built per-instance indexes into the `cache_dir/<type>` layout. |
| `run_edit_audit.py` | wrap an edit command with before/after diff artifacts, dirty-start preimages, a generated revert script, and optional verification command metadata. The same library also exposes `run_agent_with_edit_audit()` for wrapping an `AgentRunner.run()` edit attempt and persisting `agent_result.json` alongside the diff/revert artifacts; if the runner raises, it still finalizes audit/revert artifacts and writes `agent_error.json`. |

The agent-localization scorer (answer + `read` paths + retrieval nodes →
files@k / symbols@k) lives in
`codeminer/eval/retrieval_eval.py:score_agent_localization`, shared by the
runner and the offline ablations.

**Not here:** the offline *retrieval* ablations (no agent, no LLM) live in
`scripts/retrieval_ablation/` (`graphrag_retrieve`, `graph_recall_ablation`,
`index_compare`) — they ask "what is the graph/index worth as a *retriever*",
orthogonal to the agent's tool harness. The generic paired-comparison tool is
`scripts/analysis/compare_harnesses.py`.

## Run it

```bash
python scripts/agent_compile/run_sweep.py \
    --config scripts/agent_compile/configs/design_space.yaml \
    --output-dir results/agent_compile/design_space

python scripts/agent_compile/aggregate.py \
    --cells-dir results/agent_compile/design_space/cells \
    --output-dir results/agent_compile/design_space
```

`run_sweep.py` resumes per cell and does not persist transient (rate-limit)
failures, so a re-run retries them. Instances without prebuilt indexes are
skipped and recorded in `sweep_summary.json`.

## Milestone feedback suites

Runner milestones should use a frozen suite manifest instead of hand-picking a
new smoke every time. The current scheduled static-LSP route gate is:

```bash
python scripts/agent_compile/plan_feedback_suite.py \
    --suite-file scripts/agent_compile/feedback_suites/haiku_static_lsp_route_q2.yaml \
    --write-plan
```

The emitted plan has three commands: route-lifecycle preflight, the bounded
Haiku synthesis sweep, and the promotion-profile aggregate. The q2 suite is a
24-cell local gate: Python/Go/Rust traversal, two queries per instance/category,
two arms, and two reps.

## How indexes reach the agent

Prebuilt per-instance indexes: `lib/prebuilt.py` symlinks the prebuilt `vector`
+ `graph.pkl` under `prebuilt_dir/<instance>/` into the `cache_dir/<type>`
layout and builds BM25 fresh, then `build_skill_contexts(rebuild=False)` *loads*
them — no cloning/reindexing, no `RepoManifest`.
