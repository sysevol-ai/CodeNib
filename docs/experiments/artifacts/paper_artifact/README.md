# CodeMiner Paper Artifact (Core)

This artifact supports the measurements and figures in "CodeMiner: A
Materialized Repository Substrate for Coding Agents." It is a retained-result
artifact for auditing the reported estimators and rebuilding Figures 2--8. It
does not claim turnkey re-execution of historical cloud model calls or a
comparison against an unmaterialized competing system.

## Contents

- `inputs/datasets/base`: the frozen 100-row CodeMiner Base parquet.
- `inputs/datasets/synthesis`: five 100-row synthesis configurations and their
  quality report.
- `inputs/profile_log` and `inputs/eval_results`: dense construction, query,
  retrieval, and reranking records.
- `inputs/dense_graphrag_matrix_v4`: development/test graph-ablation protocol
  and records.
- `inputs/faiss_index_family_qwen06_100.json`: the controlled FAISS family
  ablation.
- `inputs/lsp_replay_base_v3_100`: frozen LSP requests, setup fields, and all
  replay reports.
- `inputs/materialized_trace_batch_v9`: the lifecycle plan, exact runtime
  source archive, environment capture, traces, per-snapshot reports and logs,
  summary, and independent analyses.
- `inputs/agent`: all selected Haiku and Qwen policy records, including failed,
  retried, and forced-format trajectories retained by the analysis.
- `figure`: pinned plotting dependencies, plotting sources, and the one-command
  reproduction driver.
- `verification/expected`: canonical JSON and PNG outputs used by the figure
  verifier.

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
python -m venv .venv
. .venv/bin/activate
python -m pip install -r figure/requirements-paper.txt
PYTHONDONTWRITEBYTECODE=1 python figure/reproduce_paper_figures.py \
  --config paper_artifact_config.json \
  --output-dir "$REPRODUCED_FIGURE_ROOT"
PYTHONDONTWRITEBYTECODE=1 python figure/verify_paper_figures.py \
  --expected-dir verification/expected \
  --actual-dir "$REPRODUCED_FIGURE_ROOT"
```

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
