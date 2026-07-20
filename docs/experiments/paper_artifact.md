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
the experiment data root. The 40-transition manifest, graph protocol-21 rows,
vector protocol-4 rows, stability audit, and schema-2 aggregate live in
`docs/experiments/artifacts/incremental_maintenance_v1/`. The manifest packages
them with the current Python runtime, graph/vector/stability runners,
aggregator, focused tests, and build metadata. The core archive does not
duplicate external repositories, model weights, or language toolchains.

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

Then run the reviewer entry point from the bundle root. Write generated files
outside the bundle so its complete checksum inventory remains valid:

```bash
python -m venv "$ARTIFACT_FIGURE_VENV"
. "$ARTIFACT_FIGURE_VENV/bin/activate"
python -m pip install -r figure/requirements-paper.txt
python artifact_eval.py smoke
python artifact_eval.py full --output-dir "$ARTIFACT_EVAL_OUTPUT"

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

`core-rc27` supersedes RC26 because the paper now reports five agent models and
the 40-transition graph/vector maintenance study. Its manifest SHA-256 is
`6e5de712a9044a7d0cb098b8293e84310764560cc0056b71f29975ea2208d302`;
its source-lock SHA-256 is
`ef93918918611f5d1d2c094fab54638159e58a68741092d8ec1cd651cfd843cb`.
The verified bundle has 10,556 payload files and 263,940,010 bytes. Provenance
pins CodeMiner commit `642008ab973ff2965cedef0f3d2df9b2aa01f2bf` and figure
commit `a0336c91bcd517164838d17dcb0e2a55c685207c`.

An extracted-bundle full run passes 15 estimator tests, reproduces and matches
all 18 deterministic outputs, passes all 146 semantic claims, and verifies
embedded fonts in 15 PDFs. The archive is 21,532,067 bytes with SHA-256
`be4cbfd557364d3d225434297058919e26fbfae4ce397140a35b7081b78e1466`.
This is a local release candidate, not a public artifact URL. Rebuild it after
any selected input, figure program, expected output, or bundled document
changes.
