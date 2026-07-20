# Incremental View Maintenance

This study evaluates whether CodeMiner can advance materialized repository
views across real commits without changing the declared serving result. It uses
one frozen commit-transition manifest for graph and vector maintenance, while
respecting each view's source-selection contract.

## Frozen workload

`docs/experiments/artifacts/incremental_maintenance_v1/transitions.json`
contains 40 first-parent transitions: five transitions for each of eight
repositories, with two repositories per language across Go, Python, Rust, and
TypeScript/JavaScript. Its manifest identity is computed from canonical JSON by
`codeminer.eval.maintenance_manifest.manifest_id`; runners write that identity
into every result row.

The manifest stores graph-backend language families. View runners lower those
families through the central registry; in particular, `ts` expands to both the
JavaScript and TypeScript chunkers rather than silently dropping `.js`/`.jsx`
or `.ts`/`.tsx` files.

Each transition records two delta scopes:

- `delta_{files,lines,symbols}` covers language source files, including tests,
  and is the graph-maintenance scope.
- `delta_{files,lines,symbols}_test_excluded` follows the vector builder's
  default `filter_tests=True` contract and is the vector-maintenance scope.

No-source transitions remain in the execution trace but are excluded from the
source-changing speedup population. This rule is applied before observing
latency.

## Graph protocol

The current runner emits graph protocol version 22. The completed 40-transition
campaign is frozen as protocol 21; version 22 adds the unchanged-declaration
hierarchy guard described below and therefore forms a separate analysis
population. Each row fingerprints the graph route, schema, strategy timeout,
strict RPC timeout/retry policy, attempt budget, full-oracle request
concurrency, and replay budget. Resume rejects rows from earlier versions or a
different fingerprint so provider normalization, impact closure, and timing
contracts cannot be pooled. Base artifact protocol 17 additionally requires lossless
buffering of coalesced JSON-RPC frames, preserving progress notifications and
responses that arrive in the same pipe read. Full builders wait for workspace
readiness before opening the repository-wide document-symbol stream, then use
a second barrier before collecting references. Reference materialization sends
ordinary LSP requests through a bounded 10-request window and restores results
to definition order; it does not use the optional JSON-RPC batch payload. This
allows the provider to schedule independent lookups without changing the query
set or graph semantics. The same window applies to independent reverse-reference
candidates in incremental repair, so the comparison does not penalize either
arm with avoidable client-side serialization.

For Rust, CodeMiner pins the rust-analyzer binary independently from the
project build toolchain. Live LSP launches the selected analyzer by absolute
path while allowing Cargo and proc macros to honor the repository's
`rust-toolchain.toml`; `CODEMINER_RUST_PROJECT_TOOLCHAIN` is the explicit
override. This avoids forcing a modern analyzer's Rust toolchain onto older
repository snapshots. SCIP generation retains its separately controlled
toolchain contract.

Every target commit executes three independent arms from the same base graph
and the same live-LSP provider contract:

1. `fully-rebuild`: collect every file's definitions first, then materialize
   declaration-side references after the workspace is indexed, and construct a
   fresh graph.
2. `file-level-patch`: replace graph facts for each changed file.
3. `symbol-level-patch`: repair only changed symbols and affected relations.

Incremental latency includes change detection, repair, and persistence. The
runner separately reports LSP server startup and a one-time workspace warmup;
their sum is also amortized over the five-transition chain. Neither component
is hidden inside per-transition repair latency. File and symbol arms are
deterministically counterbalanced across repositories.

The symbol arm uses changed-range semantic tokens, lexical candidates for
navigable syntax omitted by semantic-token providers, and declaration-side
reference reconciliation. Interactive graph queries keep bounded candidate
expansion, but strict materialization checks every exact-name candidate and
refreshes all workspace incoming references for declarations affected by the
diff, including anchors in unchanged files. New and replaced files also
receive a file-scope lexical supplement for navigable
syntax, such as imports and documentation links, that semantic-token providers
can omit. Duplicate flattened symbol identities fall back to file replacement
only when an edited hunk makes the identity ambiguous. Protocol 22 also falls
back when an old-only and new-only symbol share the same mapped declaration
coordinate and leaf name but differ in hierarchy. This prevents a provider's
parent-symbol drift from preserving a stale identity. These are correctness
mechanisms, not extra retrieval arms: they reproduce the full builder's
declaration-reference materialization contract.

Strict materialization uses a 120-second request budget and one retry for actual
timeouts or server cancellations. The budget applies uniformly to document
symbols, definitions, references, and semantic tokens. JSON-RPC `null` remains
the valid no-result response and is not retried. A fresh rebuild fails if the
strict reference oracle still times out; incomplete reference graphs are never
admitted as successful baselines. A final non-null request failure poisons an
incremental arm instead of being interpreted as an empty result. The short
hover readiness probe is not a graph query: its timeout only causes polling to
continue and never creates or removes graph facts.

Each base build, patch phase, and per-commit rebuild permits at most two clean
strategy attempts. A failed patch attempt is discarded and replayed from the
base graph with a new LSP process. Rows record the attempt count, failed-attempt
details, recovery wall time, and fault-inclusive amortized latency. The primary
latency describes the successful attempt; recovery and failure rates are
reported separately. Resume requires every declared arm, its fidelity result,
and the rebuild oracle, so a missing file baseline cannot become terminal.

Correctness is reported at two layers. The serving layer replays deterministic
static definition/reference requests and requires exact responses. The
materialization layer compares vertex and typed anchored-edge multisets, both
globally and on facts touching changed files. A strict speedup is admitted only
when both layers are exact; failed guards remain result rows with a null guarded
speedup. Raw fidelity and unguarded latency distributions remain adjacent to
the strict result, without inventing an acceptance threshold. Reporting both
layers matters because an LSP provider can expose a
stable but malformed document-symbol selection range. In that case an
incremental graph can be serving-equivalent while declining to reproduce a
provider-coordinate artifact in the fresh graph.

Independent same-commit rebuilds audit the live provider separately from the
maintenance algorithm. The stability analysis reports graph F1, serving-result
agreement, and exact counts without declaring a post hoc pass threshold. A
patch-versus-fresh mismatch is therefore interpreted next to the provider's
own fresh-versus-fresh variation, rather than assuming one cold rebuild is a
deterministic oracle.

Persisted symbol identities are canonical across the full LSP decoder, the
incremental patcher, and the corresponding SCIP decoder; for example, Go
receiver syntax is normalized before graph construction. The `active`
SCIP/clangd route is an explicit backend comparison, not the
correctness oracle for LSP maintenance. Comparing a live-LSP patch against a
static SCIP rebuild would otherwise confound maintenance error with provider
semantic differences. Repository dependency preparation and model/tool startup
are outside per-transition maintenance latency and are reported separately.
Graph schema v5 persists both the declaration line and identifier character;
older graph pickles fail closed instead of silently replaying an LSP request at
column zero. The runner fixes console logging at `WARNING` so arm latency does
not depend on strategy-specific debug volume.

C/C++ is not pooled into this three-arm comparison because its clangd `.idx`
backend exposes one backend-incremental path rather than distinct file- and
symbol-level modes. It can be reported as a separate backend case study.

## Vector protocol

Result rows use vector protocol version 4. Each row carries a canonical
experiment-configuration fingerprint over the model, provider, revision and
embedding arguments, levels, metric, delta threshold, and replay settings.
Resume accepts only exact two-arm rows with that fingerprint, so results from
different embedding contracts cannot be pooled. The protocol also includes
explicit empty-level clearing and rejects failed or inexact rows as
checkpoints.

Every target commit executes two independent arms with one shared, preloaded
embedding model:

1. `fully-rebuild`: rechunk, embed, and persist a fresh Flat FAISS artifact.
2. `incremental`: apply the same repository chunker contract to changed files,
   reuse content-addressed vectors, embed cache misses, and mutate or rebuild
   FAISS according to the declared delta threshold.

Arm order is deterministically counterbalanced per `(repository, step)` so GPU
and filesystem warm-up do not always favor one arm. Model load is measured once
and excluded from both update arms. The result records change detection,
rechunking, embedding, FAISS update, cache pruning, cache-hit fraction, and the
actual FAISS update mode.

A vector speedup is admitted only when each level has the same document-identity
multiset, reconstructed vectors are numerically equal, and deterministic Flat
top-k replay has exact ordered results. The primary study uses
Qwen3-Embedding-0.6B
at revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, a 2,048-token
maximum sequence length, batch size 32, and L2. The sequence cap is part of the
embedding contract: without it, a long code chunk can drive attention memory to
the GPU limit and make the workload depend on allocator state. Benchmark runs
fix console logging at `WARNING` and disable embedding progress output so full
rebuilds do not pay more terminal I/O than incremental updates. A second model
is a sensitivity analysis, not another primary arm.

## Reproduction

Freeze the transition workload:

```bash
python scripts/profiling/profile_incremental_graph.py \
  --config docs/experiments/configs/incremental_maintenance_multilang.json \
  --steps 5 \
  --write-transition-manifest \
    docs/experiments/artifacts/incremental_maintenance_v1/transitions.json \
  --manifest-only
```

Run graph maintenance with the managed SCIP/LSP toolchain on `PATH`:

```bash
eval "$(make -s print-active-scip-env)"
python scripts/profiling/profile_incremental_graph.py \
  --transition-manifest \
    docs/experiments/artifacts/incremental_maintenance_v1/transitions.json \
  --graph-route lsp \
  --timeout-s 3600 \
  --lsp-request-timeout-s 120 \
  --lsp-reference-retries 1 \
  --lsp-request-concurrency 10 \
  --strategy-attempts 2 \
  --output /mnt/data/codeminer/results/incremental_graph_multilang_v1/results.jsonl \
  --out-root /mnt/data/codeminer/results/incremental_graph_multilang_v1/work \
  --lsp-profile-dir /mnt/data/codeminer/results/incremental_graph_multilang_v1/lsp
```

Run vector maintenance:

```bash
HF_HOME=/mnt/data/codeminer/hf_cache \
python scripts/profiling/profile_incremental_vector.py \
  --transition-manifest \
    docs/experiments/artifacts/incremental_maintenance_v1/transitions.json \
  --output /mnt/data/codeminer/results/incremental_vector_multilang_v1/results.jsonl \
  --work-root /mnt/data/codeminer/results/incremental_vector_multilang_v1/work \
  --embedding-kwargs \
    '{"revision":"97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3","default_batch_size":32,"max_seq_length":2048,"encode_kwargs":{"show_progress_bar":false}}'
```

Audit provider repeatability on representative successful graph rows:

```bash
python scripts/profiling/profile_graph_rebuild_stability.py \
  --transition-manifest \
    docs/experiments/artifacts/incremental_maintenance_v1/transitions.json \
  --graph-results /mnt/data/codeminer/results/graph/results.jsonl \
  --baseline-work-root /mnt/data/codeminer/results/graph/work \
  --output-root /mnt/data/codeminer/results/graph-stability/work \
  --output /mnt/data/codeminer/results/graph-stability/results.jsonl \
  --case astral-sh__ruff-8204:1 \
  --case tokio-rs__tokio-4898:2 \
  --case preactjs__preact-4453:3 \
  --case axios__axios-4890:5 \
  --repeats 1
```

The graph and vector result files are append-safe at row boundaries. On vector
resume, completed steps are replayed to reconstruct the content cache and
incremental index state but are not appended again. Preserve failed rows; do
not replace them with selected reruns when computing the admission rate.

Aggregate partial or complete result files with the frozen manifest as the
completeness oracle:

```bash
python scripts/profiling/aggregate_incremental_maintenance.py \
  --manifest \
    docs/experiments/artifacts/incremental_maintenance_v1/transitions.json \
  --graph-results /mnt/data/codeminer/results/graph/results.jsonl \
  --graph-stability-results \
    /mnt/data/codeminer/results/graph-stability/results.jsonl \
  --vector-results /mnt/data/codeminer/results/vector/results.jsonl \
  --output-json /mnt/data/codeminer/results/incremental_maintenance_summary.json \
  --output-markdown /mnt/data/codeminer/results/incremental_maintenance_summary.md
```

The aggregate recomputes guarded ratios from raw arm times, so adding derived
fields does not require rerunning an expensive workload. It reports the
pre-declared source-changing denominator, graph serving and materialization
exactness, strict-equivalence admission rate, median/IQR and geometric-mean
speedups, and manifest completeness overall and by language.

The frozen paper population and its hashes live under
`docs/experiments/artifacts/incremental_maintenance_v1/results/`. To reproduce
the checked-in summary without external paths, pass `graph_v21.jsonl`,
`graph_stability_v1.jsonl`, and `vector_v4.jsonl` from that directory to the
same aggregation command and compare the output with `summary_v2.json`.
