# Paper Artifact Workflow

The paper artifact has two ownership layers:

1. `codeminer.eval.artifact_bundle` is general release infrastructure. It can
   be reviewed and merged into `main` independently of the paper.
2. `artifact/` freezes the paper-specific release manifest, plotting sources,
   expected outputs, container entry point, and reviewer documentation. This
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
`eval_results/`, and the selected directories under `results/`. This path is a
release-builder binding and is not committed. Reviewer plotting sources and
canonical expected outputs are vendored under `artifact/`, so assembling the
release no longer requires a separate cm-draw checkout.

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
  --manifest artifact/bundle-manifest.json \
  --root code="$PWD" \
  --root data="$CODEMINER_EXPERIMENT_ROOT" \
  --root base_dataset="$BASE_DATASET_ROOT" \
  --root synthesis_dataset="$SYNTHESIS_DATASET_ROOT" \
  --output /tmp/paper-artifact-source-lock.json
```

Compare the generated lock with
`artifact/source-lock.json`. A mismatch means
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
  --manifest artifact/bundle-manifest.json \
  --source-lock artifact/source-lock.json \
  --root code="$PWD" \
  --root data="$CODEMINER_EXPERIMENT_ROOT" \
  --root base_dataset="$BASE_DATASET_ROOT" \
  --root synthesis_dataset="$SYNTHESIS_DATASET_ROOT" \
  --output "$ARTIFACT_BUILD_ROOT/codeminer-paper-artifact"

python -m codeminer.eval.artifact_bundle verify \
  --bundle "$ARTIFACT_BUILD_ROOT/codeminer-paper-artifact"
```

Then run the reviewer entry point from the bundle root. The native command
creates an isolated virtual environment outside the bundle; the Docker command
runs the same evaluator as a non-root user. Both require a new output path
outside the bundle so its complete checksum inventory remains valid:

```bash
./run.sh smoke
./run.sh full "$ARTIFACT_EVAL_OUTPUT"
./run.sh docker "$ARTIFACT_DOCKER_OUTPUT"

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

`core-rc28` retains the RC27 experiment inputs while moving the complete
reviewer program into the repository-level `artifact/` directory. It vendors
the plotting source and expected outputs, removes machine-local plotting
defaults, and adds native and Docker one-command entry points. No experiment was
rerun for this release; the retained input identities are unchanged from RC27.

The frozen release identities are:

- CodeMiner: `0e49faa58f3ed7b6f60d5e52dafe1c7759088bbd`
- vendored figure source: `ae4ed0cbddf38edb3512c972b1cf271d55978bde`
- manifest SHA-256:
  `60823dead821a0364ddeac3a9bba6b0d42677040e8e8bb6c216e0965f7790d23`
- source-lock SHA-256:
  `65ca3f7c8bafc652d9ae1613e4a795d60b88c7b8a1a01a7ff4ca6dd180f8c31a`
- staged bundle: 10,561 files and 263,713,638 bytes
- `codeminer-paper-artifact-core-rc28.tar.gz`: 21,529,660 bytes, SHA-256
  `a6d9446e0473733e7d0994638a0d7b8220d51c71744a814e1a291f76bf422a73`

The native and Debian 12 Docker entry points both completed the full reviewer
workflow: 21 figure tests, 15 regenerated figure groups, three exact structured
outputs, 146 passing paper claims, and embedded-font audits for all 15 PDFs.
Native PNGs matched the canonical SHA-256 values exactly. Docker PNGs passed the
bounded cross-font dimension, perceptual-hash, and thumbnail-error checks. The
final archive was independently extracted; all 10,561 `SHA256SUMS` entries and
the extracted smoke test passed. This remains a local release candidate until a
public artifact URL is assigned.
