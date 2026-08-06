<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

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
codenib-base test split with the runner's neutral default prompt (no arm is
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
| `composer` | codenib_context (one-call GraphRAG) |
| `everything` | the full kept skill set at once |

`defaults_only` names the four default **tool** ids so the allowlist is
non-empty (an empty subset trips AgentRunner's empty→full-registry fallback);
they aren't skills, so they expose exactly the always-on tools. Toggle the
composer's graph expansion off with `CODENIB_COMPOSER_NO_GRAPH=1` for an
ablation.

## Scripts

| file | purpose |
|---|---|
| `feedback_plan.py` | choose a small deterministic feedback slice: fixed smoke cases plus a seed-rotated holdout, stratified by language/group. Outputs JSON instance lists for `run_sweep.py --instances ...`. |
| `run_sweep.py` | thin CLI for `codenib.eval.agent_runner.sweep.run_sweep`: `{arms} × {instances} × {reps}` agent cells on prebuilt indexes; `cells/<id>.json` + `sweep_summary.json`. |
| `run_synthesis_sweep.py` | thin CLI for `codenib.eval.agent_runner.query_sweep.run_query_sweep`: many query rows per prebuilt repo, loaded from `sysevol-ai/codenib-synthesis`; `cells/<id>.json` + `synthesis_summary.json`. |
| `feedback_summary.py` | thin report CLI for `codenib.eval.agent_runner.feedback_summary`: arm summaries, baseline deltas, context-source counts, and runtime failure groups for small feedback runs. |
| `aggregate.py` | thin report CLI for `codenib.eval.reports.cost_arm_report`: per-arm metrics, skill-invocation histogram, LSP-route adoption, easy/hard split, per-scenario cells, Pareto front → `report.md` + `metrics.json`. |

The base agent-localization scorer (answer + `read` paths + retrieval nodes →
files@k / symbols@k) lives in
`codenib/eval/retrieval_eval.py:score_agent_localization`, shared by the
runner and the offline ablations. Sweep cell scoring glue (span metrics, format
failure, pre-load contribution) lives in `codenib.eval.agent_runner.scoring`.

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

For fast iteration, plan a small feedback slice first and pass the emitted
`instances` list to `run_sweep.py --instances ...`. Keep the same seed for a
fixed smoke gate; rotate the seed for a fresh holdout.

```bash
python scripts/agent_compile/feedback_plan.py \
    --config scripts/agent_compile/configs/design_space.yaml \
    --seed 2026w27 \
    --smoke-per-group 1 \
    --holdout-per-group 2 \
    --output-json results/agent_compile/feedback_plan.json

python scripts/agent_compile/feedback_summary.py \
    results/agent_compile/design_space \
    --baseline defaults_only \
    --output-json results/agent_compile/design_space/feedback_summary.json
```

`run_sweep.py` resumes per cell and does not persist transient (rate-limit)
failures, so a re-run retries them. Instances without prebuilt indexes are
skipped and recorded in `sweep_summary.json`.

### Provenance: the summary `protocol` block

`synthesis_summary.json` embeds a `protocol` block so a synthesis sweep is
reproducible from its artifact alone: `sweep_id`, `model` / `model_revision`,
`dataset` / `dataset_revision`, `split`, `subsets`, `preload`, `reps`,
`max_turns`, token/temperature settings, `embedding_model` /
`embedding_revision`, `topk`, the category/query/instance caps,
`query_selection`, and the free-form `run_metadata` mapping. The `*_revision`
and `run_metadata` fields come from the sweep config
(`codenib.eval.agent_runner.sweep_config.SweepConfig`); `model_revision` is
*recorded*, not enforced — immutable local revisions must also be enforced when
launching the model server.

Per-instance query capping (`--max-queries`) is deterministic:
`run_query_sweep` supports `dataset_order` (first N rows in dataset order —
what `run_synthesis_sweep.py` uses) and `category_round_robin` (round-robin
across sorted categories, starting category rotated by instance index); the
strategy used lands in `protocol.query_selection`. On a resumed run,
already-written cells are listed under the summary's `cached` key instead of
being re-run, so the summary still accounts for every cell.

## Reusable Code Boundary

Reusable config, harness, sweep execution, per-query sweep execution, preload,
orchestration, scoring, and baseline helpers live in
`codenib.eval.agent_runner`. `scripts/agent_compile` owns experiment CLIs,
configs, dataset selection, and report glue only; the old
`scripts.agent_compile.lib` compatibility namespace has been removed.

## How indexes reach the agent

Prebuilt per-instance indexes:
`codenib.eval.agent_runner.prebuilt` symlinks the prebuilt `vector` +
`graph.pkl` under `prebuilt_dir/<instance>/` into the `cache_dir/<type>` layout
and builds BM25 fresh, then `build_skill_contexts(rebuild=False)` *loads* them
— no cloning/reindexing, no `RepoManifest`.
