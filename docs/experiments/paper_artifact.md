# Paper Artifact Workflow

The paper artifact has two ownership layers:

1. `codeminer.eval.artifact_bundle` is general release infrastructure. It can
   be reviewed and merged into `main` independently of the paper.
2. `docs/experiments/artifacts/paper_artifact/` freezes the paper-specific
   datasets, result trees, plotting sources, and release documentation. This
   directory belongs on the publication artifact branch until artifact
   evaluation begins.

## Branch And Promotion Policy

Use `artifact/system-paper-eval` as the integration branch for publication
work. A change belongs there when it fixes a paper protocol, names a frozen
dataset or model, stages retained results, renders a paper figure, or updates a
release manifest. Generated figures and fixed experiment outputs do not belong
on `main`.

Promote a change to `main` only when it is useful independently of the paper.
Typical examples are runtime and index behavior, stable MCP or manifest
contracts, dataset-independent evaluation utilities, and their tests and user
documentation. Promotion uses a focused branch based on current `main`, with
only the reusable change and its smallest relevant tests. After that PR merges,
bring the resulting `main` commit back into the artifact branch; do not merge
the artifact branch itself into `main`.

Each release candidate remains pinned to the exact CodeMiner and figure-source
commits recorded in its source lock. Later artifact-branch development does not
mutate an existing release candidate and does not require rebuilding it unless
a selected input, plotting source, estimator, or bundled document changes.

Large experiment outputs are inputs, not source code. They may live on a local
disk, object store, or downloaded release, but no mount point is encoded in the
manifest. The manifest refers only to named roots and relative paths. A source
lock records the canonical file count, byte count, and path-sensitive SHA-256
identity of every selected tree.

## Prepare Dataset Roots

Freeze the two public datasets at the revisions used by the paper. The output
directories can be anywhere outside the repository.

```bash
hf download fishmingyu/codeminer-base-dataset \
  data/test-00000-of-00001.parquet \
  --repo-type dataset \
  --revision 4eb84e2e8918474969ce68c5b06facf14d6be604 \
  --local-dir "$BASE_DATASET_ROOT"

hf download sysevol-ai/codeminer-synthesis \
  --repo-type dataset \
  --revision 5ac36c39ef69bbfe2e14dac58b6067b8c350c53e \
  --include '.gitattributes' 'README.md' 'quality_report.json' \
    '*/test-00000-of-00001.parquet' \
  --local-dir "$SYNTHESIS_DATASET_ROOT"
```

`$CODEMINER_EXPERIMENT_ROOT` is the root containing `profile_log/`,
`eval_results/`, and the selected directories under `results/`.
`$CODEMINER_FIGURE_ROOT` is the `codeminer-figure` checkout in cm-draw. These
paths are runtime bindings and must not be committed.

The graph input set includes both the development weight sweep and the final
embedding matrix. The figure reproducer checks that the development results
uniquely select weight 0.5 under the documented File Success@10, @5, @1
lexicographic rule before rendering the held-out effects.

The retained LSP evidence includes the 100-snapshot replay reports and the
development/confirmatory failure-aware analyses from the separate 540-cell
agent study. The latter support only the reported adoption count; they are not
pooled into the replay latency distribution.

The incremental-maintenance evidence is source-owned rather than mounted under
the experiment data root: protocol-v4 JSONL lives in
`docs/experiments/artifacts/incremental_graph_v4/`, and the manifest packages it
with the current Python runtime, benchmark entry point, focused tests, and build
metadata. The core archive does not duplicate the two external Git repositories
or their LSP/SCIP toolchains.

## Lock Sources

Regenerate the lock only when intentionally changing a reported input or a
plotting source. Review the lock diff before building a release.

```bash
python -m codeminer.eval.artifact_bundle lock \
  --manifest docs/experiments/artifacts/paper_artifact/bundle-manifest.json \
  --root code="$PWD" \
  --root data="$CODEMINER_EXPERIMENT_ROOT" \
  --root figures="$CODEMINER_FIGURE_ROOT" \
  --root base_dataset="$BASE_DATASET_ROOT" \
  --root synthesis_dataset="$SYNTHESIS_DATASET_ROOT" \
  --output /tmp/paper-artifact-source-lock.json
```

Compare the generated lock with
`docs/experiments/artifacts/paper_artifact/source-lock.json`. A mismatch means
the selected evidence changed; do not bypass it by building without a lock.

The synthesis Hub snapshot retains a historical `README.md` that describes an
earlier 280-row state. The artifact-level README identifies the five 100-row
parquets and `quality_report.json` as the canonical paper selection while
preserving the source README unchanged for provenance.

## Build And Verify

Build outside the source and input trees. The builder refuses an existing
output directory, stages atomically, rejects symbolic links, verifies every
locked source, and records every bundled file in `SHA256SUMS`.

```bash
python -m codeminer.eval.artifact_bundle build \
  --manifest docs/experiments/artifacts/paper_artifact/bundle-manifest.json \
  --source-lock docs/experiments/artifacts/paper_artifact/source-lock.json \
  --root code="$PWD" \
  --root data="$CODEMINER_EXPERIMENT_ROOT" \
  --root figures="$CODEMINER_FIGURE_ROOT" \
  --root base_dataset="$BASE_DATASET_ROOT" \
  --root synthesis_dataset="$SYNTHESIS_DATASET_ROOT" \
  --output "$ARTIFACT_BUILD_ROOT/codeminer-paper-artifact"

python -m codeminer.eval.artifact_bundle verify \
  --bundle "$ARTIFACT_BUILD_ROOT/codeminer-paper-artifact"
```

Then run the figure reproduction and semantic verifier from the bundle root.
Write generated files outside the bundle so its complete checksum inventory
remains valid:

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

python -m codeminer.eval.artifact_bundle archive \
  --bundle "$ARTIFACT_BUILD_ROOT/codeminer-paper-artifact" \
  --output "$ARTIFACT_BUILD_ROOT/codeminer-paper-artifact.tar.gz"
```

The core artifact intentionally excludes materialized vector, graph, BM25, and
LSP caches. They are large, source-linked, and not read by a reported estimator
or plotting program. The retained lifecycle reports still expose setup,
hydration, query, resource, and failure measurements. An optional cache archive
can be published separately, with its own license review and checksum.

## Current Release Candidate

Local `core-rc26` clarifies the agent context-policy labels, the compaction
trigger, and the Recall@5 admission reference. Relative to RC25, only the
manifest, figure-program, and expected-figure source groups changed; no retained
measurement, runtime source, dataset, estimator, or raw claim input changed.
Its manifest SHA-256 is
`b749fce51cc67d37f2e22a457aa4e0ea285a567a6ef4c36d0b80eda6bfea754b`;
its source-lock SHA-256 is
`3b1db09ffb7517b98405cdaac628c67893c74be50cfb5829feaa839f08960e38`.
The verified bundle has 7,520 files and 242,338,739 bytes. Its provenance pins
CodeMiner commit `6ca9e83d816762495baccb7b47f58060b2d6537f` and figure-source
commit `e210e92d9f257f000841178e70e75a58ef9c35e3`. The bundled runtime
suite passes 201 tests with seven skips, and the estimator suite passes seven
tests. A clean 15-job rebuild matches all 18 deterministic outputs, passes all
133 semantic claims, and embeds fonts in all 15 PDFs.

Two independent deterministic archives are 19,457,399 bytes with SHA-256
`5ff2cbc4a252d74625c91faa7200689dd27d7125931f5f57d03949b956f1f226`.
This is a local release candidate, not a public artifact URL.
