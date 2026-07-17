# CodeMiner Paper Artifact (Core)

This artifact supports the measurements and figures in "CodeMiner: A
Multi-View Data System for Serving Repository Context to Coding Agents." It is
a retained-result artifact for auditing the reported estimators, rebuilding
generated Figures 4--10, and inspecting the incremental-maintenance case study.
Figure 4 is a data-free mechanism schematic; Figures 5--10 consume the retained
measurements. The artifact does not claim turnkey re-execution of historical
cloud model calls or a comparison against an unmaterialized competing system.

## Contents

- `inputs/datasets/base`: the frozen 100-row CodeMiner Base parquet.
- `inputs/datasets/synthesis`: five 100-row synthesis configurations and their
  quality report.
- `inputs/profile_log` and `inputs/eval_results`: dense construction, query,
  retrieval, and reranking records.
- `inputs/dense-model-scale.json`: revision-bound parameter counts, output
  dimensions, and config/weight identities for the five profiled embedders.
- `inputs/dense_graphrag_dev_reference_rrf_sweep_v2.json`: the 58-snapshot
  development sweep that selects graph weight 0.5 by File Success@10, then
  @5, then @1.
- `inputs/dense_graphrag_matrix_v4`: the frozen 42-snapshot held-out graph
  analysis inputs plus the complete five-embedding result matrix.
- `inputs/faiss_index_family_qwen06_100.json`: the controlled FAISS family
  ablation.
- `inputs/lsp_replay_base_v3_100`: frozen LSP requests, setup fields, and all
  replay reports.
- `inputs/lsp_agent_base_haiku_{development,confirmatory}_v2_analysis.json`:
  failure-aware summaries for the separate 540-cell natural-adoption study;
  together they record four live-arm LSP calls.
- `inputs/incremental_graph_v4`: protocol-v4 JSONL for the five-position
  scikit-learn and Caddy chains, including the retained no-source position and
  every passing or failing file-level arm.
- `inputs/materialized_trace_batch_v9`: the lifecycle plan, exact runtime
  source archive, environment capture, traces, per-snapshot reports and logs,
  summary, and independent analyses.
- `inputs/agent`: all selected Haiku and Qwen policy records, including failed,
  retried, and forced-format trajectories retained by the analysis.
- `runtime`: the source-locked CodeMiner Python runtime, incremental benchmark
  entry point, focused tests, and build metadata used by the case study.
- `figure`: pinned plotting dependencies, plotting sources (including the
  data-free agent-runtime schematic), the one-command reproduction driver, and
  the manuscript claim-ledger builder.
- `verification/expected`: canonical JSON and PNG outputs used by the figure
  verifier, including `paper_claims.json`.

The immutable synthesis snapshot also contains its historical dataset
`README.md`, which describes an earlier 280-row state and is not the metadata
for the paper selection. The five bundled parquet files and
`quality_report.json` are canonical for this artifact: they contain 500 rows,
including five rows with non-valid generator-judge verdicts. The paper retains
those rows and reports the paired exclusion sensitivity. The source README is
preserved unchanged so its Hub snapshot identity remains auditable.

ContextBench is not part of the reported study and is intentionally absent.
See `LICENSES.md` for component-level license boundaries; the collection is not
presented under one blanket license.

The core package intentionally omits the materialized FAISS, embedding, graph,
BM25, and LSP caches. They duplicate source-linked data, are not read by any
reported estimator or figure, and are reconstructable from the frozen
repository commits and runtime source. Per-snapshot `report.json` and `run.log`
files remain, so every lifecycle measurement and failure boundary is auditable.
An optional cache archive may be published for direct replay without rebuilding,
but it is not required to reproduce the paper results.

## Audit Incremental Maintenance

The retained inputs are
`inputs/incremental_graph_v4/sklearn_results_v4.jsonl` and
`inputs/incremental_graph_v4/caddy_results_v4.jsonl`. Their SHA-256 values are
`381fb7896f9502c48ea664e69c385f62c1bc27ece6c6e959053b0b22565b2da8`
and `e0ce67a304a1e2a7a17d258e9d0886e59f3cd36633e39fe627a59fed13302ee4`.
Protocol v4 admits a speedup only after exact complete-graph and changed-slice
multiset equality and exact graph-backed definition/reference replay.

The source-locked entry point is
`runtime/scripts/profiling/profile_incremental_graph.py`. Re-execution requires
the named repository commits plus scip-python/basedpyright or scip-go/gopls, as
listed in the input README; those external repository and toolchain payloads
are not duplicated in the core retained-result archive. From `runtime`, the
focused local verification is:

```bash
ARTIFACT_ROOT="$PWD"
python -m venv "$ARTIFACT_TEST_VENV"
"$ARTIFACT_TEST_VENV/bin/pip" install "$ARTIFACT_ROOT/runtime[test]"
cd "$ARTIFACT_ROOT/runtime"
PYTHONDONTWRITEBYTECODE=1 "$ARTIFACT_TEST_VENV/bin/pytest" \
  -p no:cacheprovider test/graph/incremental \
  test/scripts/test_profile_incremental_graph.py \
  test/agent/test_lsp_graph.py \
  test/eval/test_lsp_replay_benchmark.py \
  test/scip/test_scip_go.py -q
```

Set `$ARTIFACT_TEST_VENV` outside the bundle. The bytecode and cache settings
keep the checksum-verified artifact tree read-only during this test.

## Integrity

From the artifact root, verify that every payload file is present and unchanged:

```bash
python -m codeminer.eval.artifact_bundle verify --bundle .
```

Without CodeMiner installed, the static checksum inventory can also be checked
with `sha256sum --check SHA256SUMS`. The bundle contains its exact selection
manifest, source lock, and content provenance as `BUNDLE_MANIFEST.json`,
`SOURCE_LOCK.json`, and `PROVENANCE.json`.

## Rebuild Figures

Python 3.11 was used for the verified rebuild:

```bash
python -m venv "$ARTIFACT_FIGURE_VENV"
. "$ARTIFACT_FIGURE_VENV/bin/activate"
python -m pip install -r figure/requirements-paper.txt
PYTHONDONTWRITEBYTECODE=1 python figure/reproduce_paper_figures.py \
  --config paper_artifact_config.json \
  --output-dir "$REPRODUCED_FIGURE_ROOT"
PYTHONDONTWRITEBYTECODE=1 python figure/verify_paper_figures.py \
  --expected-dir verification/expected \
  --actual-dir "$REPRODUCED_FIGURE_ROOT"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=figure \
  python -m unittest figure/test_build_paper_claims.py \
  figure/test_draw_pareto_rerank.py \
  figure/test_draw_lsp_replay.py
```

Set `$ARTIFACT_FIGURE_VENV` outside the bundle, like
`$REPRODUCED_FIGURE_ROOT`.

The driver rejects missing inputs and non-empty output directories. It writes
`reproduction_manifest.json`, including canonical SHA-256 identities for input
trees, plotting sources, command logs, and outputs. Under the pinned
environment, JSON and PNG outputs are deterministic. Matplotlib changes PDF
metadata; compare page geometry, embedded fonts, or rasterized content instead
of raw PDF hashes. Keep `$REPRODUCED_FIGURE_ROOT` outside this bundle; adding
generated files here intentionally causes the complete checksum verifier to
reject the modified copy. The driver also disables bytecode writes in every
plotting subprocess so importing `figure/plot_style.py` cannot mutate the
bundle.

`paper_claims.json` recomputes 118 manuscript-facing values from the same
frozen inputs. Each record names its source, estimator, unit, raw value,
rounding rule, and reviewed display value. Reproduction fails if any value,
sample count, guardrail outcome, or reporting precision drifts; the verifier
also checks the ledger schema, unique claim IDs, and per-claim pass state.
For Figure 7, the ledger binds model scale to the retained cache revisions and
recomputes the per-model LOC slopes and monotonicity guardrails. These are
descriptive contracts for the measured model/runtime stack, not causal claims
that parameter count or output dimension alone determines latency.
Figure 5(a) pairs File Recall@10 with the full L2 callable-index construction
timer used by the retrieval and reranking paths; a focused test rejects an L0
timer substitution.

## Provenance Boundaries

CodeMiner Base is `fishmingyu/codeminer-base-dataset` at Hub revision
`4eb84e2e8918474969ce68c5b06facf14d6be604`; its parquet SHA-256 is
`daea6f95adbf7ca4014d667c7550716c60466b9448f937d5a9fa33d676adafa2`.
CodeMiner Synthesis is `sysevol-ai/codeminer-synthesis` at revision
`5ac36c39ef69bbfe2e14dac58b6067b8c350c53e`; `SOURCE_LOCK.json` identifies
every bundled language parquet. The lifecycle experiment captures its exact
CodeMiner source archive, package freeze, hardware, CUDA state, toolchain
versions, plan, and traces. Complete Qwen agent manifests and selected-cell
hashes are retained. Older retrieval runs record model identifiers but not Hub
revisions at execution time; `legacy-model-cache-provenance.json` reports the
later cache audit and labels it as post-hoc evidence. Historical Haiku runs
retain the observed provider model ID and selected outputs but cannot reconstruct
the provider's hardware or immutable weight revision.
