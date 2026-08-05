# Evaluation Artifact Bundles

`codenib-artifact-bundle` stages content-locked evaluation releases from
explicit local sources. It is intended for retained experiment records that
are too large or too environment-specific to keep in Git.

The bundle manifest contains only named roots and relative paths. Machine paths
are supplied at runtime with repeated `--root NAME=PATH` arguments. This keeps
storage layout out of the release contract while making the selected files
reviewable.

## Manifest

```json
{
  "schema_version": 1,
  "bundle": {
    "name": "example evaluation",
    "version": "v1"
  },
  "sources": [
    {
      "id": "reports",
      "root": "results",
      "path": "batch-v1",
      "destination": "inputs/reports",
      "include": ["*.json", "*.log"],
      "exclude": ["debug-*.log"],
      "exclude_dir_names": ["cache", "__pycache__"]
    },
    {
      "id": "readme",
      "root": "release",
      "path": "README.md",
      "destination": "README.md"
    }
  ]
}
```

A directory source preserves paths below `destination`. A file source is copied
to the exact destination path. The builder rejects absolute paths, `..`,
overlapping destinations, symbolic links, non-regular files, empty selections,
and outputs nested inside selected sources.

## Freeze Inputs

Create a source lock after the experiment inputs are final:

```bash
codenib-artifact-bundle lock \
  --manifest bundle-manifest.json \
  --root results=/path/to/results \
  --root release=/path/to/release-files \
  --output source-lock.json
```

The lock stores a path-sensitive SHA-256 identity plus file and byte counts for
every source. Commit the manifest and reviewed lock, not the bound local paths.
Any later input change causes a locked build to fail.

## Build And Verify

```bash
codenib-artifact-bundle build \
  --manifest bundle-manifest.json \
  --source-lock source-lock.json \
  --root results=/path/to/results \
  --root release=/path/to/release-files \
  --output /path/to/staged-bundle

codenib-artifact-bundle verify --bundle /path/to/staged-bundle
```

The builder writes to a temporary sibling, copies each selected file while
checking for concurrent changes, generates `BUNDLE_MANIFEST.json`,
`SOURCE_LOCK.json`, `PROVENANCE.json`, and a complete `SHA256SUMS`, verifies the
payload, then atomically renames it into place. Verification rejects both
missing files and unlisted additions.

Create a portable, deterministic archive only after all release-specific
checks pass:

```bash
codenib-artifact-bundle archive \
  --bundle /path/to/staged-bundle \
  --output /path/to/staged-bundle.tar.gz
```

The archive normalizes timestamps and ownership, uses a single top-level
directory, and writes a sibling `.sha256` file. Generated analyses should be
written outside the staged bundle unless they were selected by the manifest;
otherwise the complete-inventory verifier will reject the modified copy.

## Sweep Protocol Records

Agent query sweeps self-record their protocol, so bundling a sweep's output
directory captures full provenance without extra bookkeeping. `run_query_sweep`
(`codenib/eval/agent_runner/query_sweep.py`) writes a summary JSON
(`query_sweep_summary.json` by default) whose top-level `protocol` block pins
the run configuration:

- **Identity and model**: `sweep_id`, `model`, `model_revision` — the provider
  or model-server revision recorded for experiment provenance. Immutable local
  revisions must also be enforced when launching the server.
- **Dataset**: `dataset`, `dataset_revision`, `split`, plus the applied
  filters `categories`, `max_queries`, and `max_instances`.
- **Harness**: `subsets` (the per-arm skill lists), `preload` (per-arm
  context pre-load recipes), `reps`, `max_turns`, `max_context_tokens`,
  `max_tokens`, `temperature`, and `topk`.
- **Embeddings**: `embedding_model` and `embedding_revision`. Experiments
  should set the revision whenever the provider accepts mutable model ids such
  as `main`.
- **Selection and free-form context**: `query_selection` and `run_metadata`
  (an arbitrary JSON-serializable mapping from the sweep config).

`query_selection` names the deterministic per-instance query capping strategy:
`dataset_order` takes the first `max_queries` rows, while
`category_round_robin` draws round-robin from category buckets sorted by name,
rotating the starting category per instance. Reruns of the same config
therefore select the same queries.

Alongside `protocol`, the summary records the outcome of every cell so resumed
runs are auditable: `completed` (cells run this invocation), `cached` (cells
whose per-cell JSON already existed and were reused under `resume`), `skipped`
(instances without the required prebuilt indexes, with a reason), and `failed`
(cell- or instance-level failures, each with a reason). Bundle the sweep
output directory — summary plus `cells/` — with the manifest flow above to
freeze both the protocol and the per-cell results.

## Quality-constrained cost reports

Use `codenib-quality-cost-report` to compare agent arms without dropping misses
or selecting a cheap arm that fails the localization-quality guard. Inputs may
be complete sweep directories or individual cell JSON files; sharded `cells/`
directories are discovered recursively.

```bash
codenib-quality-cost-report /path/to/baseline-and-policy-results \
  --output-dir /path/to/report \
  --arm grep_only \
  --arm preinj_eager \
  --arm preinj_eager_compact \
  --noninferiority-margin 0.05
```

The report emits JSON and Markdown with exact paired denominators, localization
success, token cost per attempt and per success, repository-clustered confidence
intervals, and the least-cost arm that clears the declared quality margin.
Recorded USD estimates remain labeled as unpinned. Offline repricing requires
complete raw token classes and an explicit content-hashed pricing snapshot;
unknown local-model prices are reported as unavailable. These values are not
GitHub or Copilot credit estimates.
