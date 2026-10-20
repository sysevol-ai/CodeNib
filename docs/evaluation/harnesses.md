<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Harnesses and metrics

This page lists the commands that produce benchmark scores and defines
each metric they report. The [Benchmarks overview](index.md) shows which
method and benchmark pairs each harness covers.

Most harnesses live in `examples/` and `scripts/` in a source checkout, not
in the installed package. Install the development extras first:

```bash
git clone https://github.com/sysevol-ai/CodeNib.git
cd CodeNib
make dev
```

## Metrics

### File and symbol metrics

Retrieval and agent harnesses report these at each cutoff *k*, using the
shared definitions in `codenib/eval/retrieval_eval.py`. Files are deduplicated
in rank order before truncation.

| Metric | Definition for one task |
| --- | --- |
| Recall@k | Fraction of target files (or symbols) that appear in the top *k* |
| Precision@k | Fraction of the top *k* predictions that are targets |
| Accuracy@k | 1 if **every** target appears in the top *k*, otherwise 0 |

Scores are averaged over tasks (macro average). Because accuracy requires every
target, it is stricter than recall on multi-file issues. The external-agent
runner reports *k* = 1, 3, 5, and 10 by default.

For the CodeNib agent, the predicted files are, in order: the files named in
its final answer, then the files it read, then files returned by retrieval
skills. External agent policies are scored on the locations they return.

### Code-block metrics

Code-block metrics compare line spans instead of names, so a prediction counts
even when its symbol name is written differently.

| Metric | Definition for one task |
| --- | --- |
| Code-block Recall@k | Fraction of ground-truth code blocks overlapped by at least one of the top *k* predicted spans |
| Code-block precision@k | Fraction of the top *k* spans that overlap any ground-truth block |

Two spans overlap when they are in the same file and their line ranges
intersect. Predicted spans that overlap an earlier prediction are dropped
before truncation, so one region cannot fill several of the *k* slots.

The CodeNib agent sweeps (`run_sweep.py`, `run_synthesis_sweep.py`) report
two scopes:

- `answer_blocks`: the spans the agent committed to in its final answer. This
  is the agent's result.
- `retrieval_blocks`: the spans its retrieval tools returned. This is
  comparable to a retrieval route's code-block recall, such as the grep → Jev
  results.

Neither scope bounds the other. An agent can answer with a region that it
found by `grep` and that retrieval never returned.

The `*_loc_agent.py` runners and `skill_agent_eval.py` report file and symbol
metrics only.

The [grep → Jev result](grep_jev.md) uses macro code-block Recall@5.

### SWE-Explore metrics

The SWE-Explore harness reports the 17 metrics registered by the pinned
official runner: `precision`, `recall`, `f1_score`, `hit_file_rate`,
`noise_file_rate`, `hit_region_rate`, `noise_region_rate`,
`weighted_core_coverage`, `context_efficiency`, `optional_coverage`,
`ndcg_at_100`, `ndcg_at_300`, `ndcg_at_500`, `recall_at_100`,
`recall_at_300`, `recall_at_500`, and `first_useful_hit`.

Two cutoffs are separate. `--top-k` limits how many regions the method
returns, while the `_at_100`, `_at_300`, and `_at_500` metrics apply a budget of
accumulated source lines. CodeNib's scorer agrees exactly with the official
evaluator; see the [validation report](swe_explore.md).

### Agent Retrieval Bench metrics

`codenib-arb` calls ARB's own scoring functions rather than reimplementing
them. Each task is scored on a ranked file list.

| Metric | Definition for one sample |
| --- | --- |
| File Recall@5, @10, @20 | Fraction of gold files among the first 5, 10, or 20 distinct files |
| MRR | Reciprocal rank of the first gold file in the full supplied ranking |
| BCY@4000 … @32000 | Gold-file coverage when the first 20 distinct files are packed into a token budget with the official tokenizer and renderer |

Primary aggregates are means over all selected samples, with failures scored
zero; valid-only means are reported next to them. See the
[metric contract](agent_retrieval_bench.md#metric-contract).

### ContextBench metrics

ContextBench scores a trajectory, not a ranked list. The pinned official
evaluator compares the source the agent actually received with the gold
context:

| Metric | Definition for one task |
| --- | --- |
| Coverage (recall) and precision | At file, symbol, byte-span, and line granularity, for the final context |
| Trajectory AUC and redundancy | Coverage accumulated over the trajectory's steps, and repeated reads, as the official evaluator defines them |
| EditLoc | Only when a real `model_patch` is supplied; the bridge never falls back to the reference patch |

Only numbered source lines that a tool actually delivered count as viewed;
file listings and name-only hits do not. The package's `final_*` summaries
are micro averages, while the leaderboard averages per-task scores; see
[preparing a leaderboard submission](contextbench.md#preparing-a-leaderboard-submission)
before comparing the two.

### OrcaLoca File Match and Function Match

`score-orcaloca` reports OrcaLoca's own unranked File Match and Function Match
instead of the ranked metrics above. The two policies are therefore not
compared under one metric. See [paired provider compatibility](../agent_integrations.md#paired-provider-compatibility).

## Harnesses

### One-shot retrieval

| Command | Methods | Datasets |
| --- | --- | --- |
| `examples/bm25_retrieve_baseline.py` | BM25 | `swebench_lite`, `locbench_v1` |
| `examples/embedding_retrieve_baseline.py` | Dense, flat or IVF FAISS index | `swebench_lite`, `locbench_v1`, `codenib_base` |
| `examples/graph_retrieve_baseline.py` | BM25 seeds plus k-hop BFS or personalized PageRank; optional `embedding` or `cross-encoder` rerank | same three |
| `examples/retrieve_rerank.py` | `--retrieval-mode auto\|dense\|sparse\|hybrid`, `--rerank-strategy llm\|decisions\|embedding\|crossencoder`, `--planning-budget fast\|balanced\|thorough` | same three |
| `examples/eval_synthesized_queries.py` | `--pipeline bm25\|embedding\|agent` | CodeNib Synthesis JSON |
| `scripts/embeddings/eval_codenib_base_embeddings.sh` | Embedding-model sweep | CodeNib Base |
| `scripts/embeddings/eval_codenib_base_rerank_matrix.sh` | Small dense first stage × embedding or cross-encoder reranker | CodeNib Base |
| `codenib-swe-explore-benchmark` | `RepositoryContextExplorer` with `--policy auto\|bm25\|dense\|hybrid\|hybrid_rerank\|graph` | SWE-Explore |
| `codenib-arb prepare\|run\|evaluate` | BM25 over the released corpus chunks; `evaluate` rescores external file rankings without retrieval | Agent Retrieval Bench |

```bash
python examples/retrieve_rerank.py \
  --dataset codenib_base \
  --eval-instances "" \
  --retrieval-mode hybrid \
  --rerank-strategy crossencoder \
  --result-path results/hybrid_crossencoder.json
```

`retrieve_rerank.py` defaults `--eval-instances` to a SWE-bench Lite
annotation file. Pass an empty value for CodeNib Base so the loader uses the
dataset's own ground-truth columns; otherwise an existing SWE-bench file
replaces them and no instance is scored.

The SWE-Explore runner needs clean checkouts at the joined base commits:

```bash
codenib-swe-explore-benchmark \
  --bench /path/to/bench.final.public.jsonl \
  --case-set docs/assets/swe_explore_cases.json \
  --repos-root /path/to/repos \
  --output results/codenib-swe-explore.json \
  --policy bm25 \
  --top-k 5,10,20
```

### Fixed LLM workflows

| Command | Method | Datasets |
| --- | --- | --- |
| `scripts/benchmark_model_grep.py` | Freezes model-planned grep candidates | CodeNib Base at a pinned revision |
| `scripts/benchmark_jev_base.py`, `scripts/benchmark_jev_dense.py` | Jev and Qwen3 rerankers over BM25, dense, hybrid, or frozen grep candidates | CodeNib Base at a pinned revision |
| `scripts/evaluate_grep_jev_product.py` | Fresh planning and Jev through the product code path; requires `--allow-billed-calls` and `--max-cost-usd` | CodeNib Base at a pinned revision |
| `examples/agentless_loc_agent.py` | Agentless v1.5.0 localization | `codenib_base`, `swebench_lite`, `locbench_v1` |

Planning and Jev scoring make billed OpenRouter calls; `benchmark_jev_dense.py`
only freezes candidates and makes no paid calls.
[grep → Jev](grep_jev.md#reproduce-and-interpret) explains which ones
produced the published numbers.

### Agents

| Command | What it compares | Datasets |
| --- | --- | --- |
| `examples/{locagent,cosil,orcaloca,claude,codex}_loc_agent.py` | One agent policy, scored with the shared file and symbol metrics | `codenib_base`, `swebench_lite`, `locbench_v1` |
| `scripts/analysis/compare_agent_integrations.py` | LocAgent or OrcaLoca with the same model and cases, swapping the `native` and `codenib` repository provider | Prepared SWE-bench Lite cases |
| `examples/skill_agent_eval.py` | The CodeNib agent with any subset of skills | SWE-bench (Lite by default), or Loc-Bench with `--dataset czlll/Loc-Bench_V1`; needs `--eval-instances` |
| `scripts/agent_compile/run_sweep.py`, `run_synthesis_sweep.py` | CodeNib agent arms from a YAML configuration: skill subsets and pre-loaded context | CodeNib Base, CodeNib Synthesis |

```bash
python examples/locagent_loc_agent.py \
  --dataset codenib_base \
  --model "$LOCAGENT_MODEL" \
  --result-path results/locagent_loc.jsonl \
  --resume
```

Agent runs are resumable with `--resume`. The run summary reports succeeded,
failed, skipped, and scored counts; check them before comparing aggregates.
The paired driver `compare_agent_integrations.py` additionally keeps missing
and failed cells in the requested denominator.

### Agent context

| Command | What it runs | Benchmark |
| --- | --- | --- |
| `codenib-contextbench prepare\|run\|evaluate` | The CodeNib agent with `read`, `grep`, `glob`, and a BM25 skill through OpenRouter; exports official trajectories and runs the pinned evaluator | ContextBench |
| `scripts/benchmark_context_agent.py prepare\|run\|repack\|refine\|evaluate` | The research harness behind the [development results](contextbench-results.md): the agent with source tools only, then optional `refine` stages such as function completion | ContextBench |
| `scripts/benchmark_context_claude.py` | CodeNib's source tools served to the Claude Agent SDK through an in-process MCP server; a different agent loop, not a transport-only ablation. The 500-task native control and the search-pair workers run here | ContextBench |
| `scripts/contextbench_reproduce_search_pair.py` | Replays the archived search-pair source footprints and official metrics from an extracted bundle, without model calls | ContextBench |

Commands that call a model require an explicit `--live` flag. The research
arms themselves, such as the search pair, worker router, and evidence review,
are modules beside these commands (`scripts/contextbench_search_pair.py` and
its neighbours); the [results page](contextbench-results.md) records how each
one was run. The
[ContextBench guide](contextbench.md) covers installation of the isolated
evaluator environment, task selection, resume, and artifacts.

### Resolution

| Command | What it runs | Benchmark |
| --- | --- | --- |
| `scripts/benchmark_context_solver.py` | A pinned mini SWE-agent repair loop on the retrieved context, one task per invocation | ContextBench Pass@1 |
| `scripts/contextbench_verified.py`, `contextbench_pro.py`, `contextbench_polybench.py`, `contextbench_multiswe.py` | Grade one solver patch in the task's native environment with pinned official scripts; no model calls | ContextBench Pass@1 |
| `python -m scripts.guardian.deepswe.ablation`, `solo_matrix` | Launch DeepSWE trials through the external harness | DeepSWE |

Resolution runs need the pinned task images and Docker. Report a Pass@1 only
with its first-attempt denominator and every unresolved or failed attempt.

## Reporting

`codenib-quality-cost-report` combines agent sweep results with token usage
and pricing to compare accuracy against cost. `codenib-artifact-bundle`
packages the inputs, metrics, and provenance of a run into a content-locked
bundle; see [evaluation artifact bundles](../evaluation_artifacts.md).

For resolution runs, `codenib.eval.benchmarks.deepswe` loads the fixed-slot
trial artifacts written by the external DeepSWE harness and aggregates pass
rates, token usage, and cost. `python -m scripts.guardian.deepswe.results`
and `export_dashboard` are its command-line front ends; neither launches the
benchmark.

## Conventions

- Ground truth reaches the scorer only after predictions are written. Runtime
  code never sees the labels.
- Pin the dataset revision for any number you intend to publish. The CodeNib
  Base loader takes no revision and reuses its cached download; the published
  scripts pass the revision explicitly.
- Count failed cases in the denominator, or report them next to the score.
- Report the model, its revision where one exists, and the candidate limits
  used for reranking.
