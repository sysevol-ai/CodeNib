<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Semantic Fact Batches

`FactBatch v1` is CodeNib's provider-neutral per-file semantic contract. Today
it is produced from native SCIP buffers, SCIP occurrence sidecars, and existing
`CodeGraph` projections for validation and convergence work. It does not sit on
the normal MCP query path. clangd, generic LSP, and lower-confidence syntax
adapters remain possible extension points rather than implemented routes.

```text
SCIP native buffer / SCIP sidecar / CodeGraph projection
                         |
                    FactBatch v1
                         |
          resolver and convergence contracts
```

## Contract

A batch represents exactly one canonical repository-relative POSIX path and
one source-content digest. It records:

- analyzer provider, profile digest, language, completeness, capabilities, and
  position encoding;
- symbol definitions with stable IDs, monikers, kinds, and zero-based half-open
  definition/selection ranges;
- exact occurrences with provider role bits;
- edges with a resolved target ID or an unresolved target moniker;
- edge kind, anchor, provenance, confidence, and resolver identity;
- bounded diagnostics.

Facts are frozen, tuple-backed, deterministically sorted, JSON-projectable, and
content-addressed. A failed batch cannot contain semantic facts. File paths and
digests fail closed, symbol IDs must be unique within the file, and an edge must
have exactly one resolved or unresolved target.

Framework and heuristic edges must name the resolver that synthesized them.
This keeps precise SCIP/LSP facts distinguishable from framework knowledge and
lower-confidence inference at query time.

## Current Adapters And Resolution

`fact_batch_from_scip_occurrences` preserves exact SCIP positions and
unresolved monikers. The current occurrence sidecar does not retain caller
attribution or symbol kinds, so its completeness is intentionally `partial`.

`fact_batches_from_code_graph` provides the transitional dual-write projection
from the existing materialized graph. Its inclusive line ranges are converted
to zero-based half-open full-line ranges, and its resolved edges retain their
source provenance. It does not change `graph.pkl`, `_SCHEMA_VERSION`, or the
C++ serialization contract.

`FactResolverPipeline` runs deterministic plugins in order. The default exact
pass resolves a moniker only when the snapshot has one definition; ambiguous
targets remain unresolved. `FrameworkRuleResolver` accepts explicit rules from
framework-specific discovery code and emits labeled, confidence-bounded edges.
The registry deliberately contains no global bag of language heuristics.
`SnapshotDefinitionResolver` is the non-mutating query-time counterpart: it
resolves a moniker only against one pinned batch set and uses a bounded,
thread-safe LRU whose key includes the caller's complete snapshot ID. Negative
and ambiguous results are cached without leaking a target across snapshots.

## Incremental Convergence

`FactOverlay` models atomic whole-file upsert/delete generations. After any
incremental sequence, `compare_fact_snapshots` compares the overlay with a clean
rebuild:

- semantic mode ignores only provider/profile route identity;
- it still checks source identity, completeness, capabilities, diagnostics,
  definitions, occurrences, ranges, edges, provenance, confidence, and resolver;
- strict mode also requires the provider and profile digests to match.

This remains the semantic parity gate. It does not replace eager graph edge
materialization for public graph queries.

## Native-Core Gate

`FactBatchBuffer v1` now implements the native boundary as little-endian
fixed-width tables plus one shared UTF-8 arena. Its `CNFB` envelope carries an
ABI version, FactBatch schema version, row counts, capability flags, and exact
graph-compatibility columns. Python validates the entire envelope before
materialization. Logical `FactBatch` construction additionally requires an
authoritative content digest for every file.

This is primarily a semantic intermediate representation, not an automatic
speedup. SCIP now decodes directly into flat native records; no C++ or Python
`CodeGraph` is needed to emit the semantic tables. Optional compatibility
columns can still recreate the exact legacy graph, while semantic-only callers
can request ownership-safe read-only buffers with no table copies.

Promotion is consumer-specific. Graph-compatible transport and eager logical
Python FactBatch tuples remain below the 20% gate. The graph-free native query
consumer passed for Python and Rust because it keeps records/postings in C++ and
does not expand them into igraph, dictionaries, or dataclasses. TypeScript
remains on the legacy graph because its measured improvement was below 20%.

C/C++ uses the same query-index consumer through a separate authoritative
adapter: a native clangd RIFF reader emits compact definition/reference records
and provider-neutral occurrence rows directly from `.idx` files. Symbol and
supported exact-position queries skip `CodeGraph`. The v3 normalization also
emits complete containment/reference adjacency plus legacy traversal order;
the hybrid route view reuses the existing tree-sitter span rules only for
touched symbols, so supported direct and query-only routes skip igraph as well.
The query index remains an ephemeral read model. The unconsumed catalog-backed
clangd FactBatch publication experiment was removed with generic storage; this
does not change graph persistence or the public query authority.

Runtime selection is explicit:

```bash
# Default: legacy graph transport
export CODENIB_CORE_FACT_BUFFER=off

# Try v1 and fall back to legacy on validation/runtime failure
export CODENIB_CORE_FACT_BUFFER=auto

# Require v1 and surface any failure
export CODENIB_CORE_FACT_BUFFER=required

# Explicit query-index API: auto promotes only measured language paths
export CODENIB_NATIVE_FACT_QUERY_INDEX=auto

# C/C++ clangd symbol/position/route queries; named fallbacks stay full-graph
export CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX=auto
```

The clangd variable remains available to the direct experiment and profiling
surface. Mutable project-local `.idx` files are not admitted in a
manifest-bound MCP or agent runtime; those contexts use the verified persisted
graph.

Run the alternating-arm parity and performance gate with:

```bash
make fact-buffer-profile \
  FACT_BUFFER_PROFILE_INDEX=/path/to/index.decoded \
  FACT_BUFFER_PROFILE_LANGUAGE=python \
  FACT_BUFFER_PROFILE_PROJECT_ROOT=/path/to/repository

make fact-query-profile \
  FACT_QUERY_PROFILE_INDEX=/path/to/index.decoded \
  FACT_QUERY_PROFILE_LANGUAGE=python \
  FACT_QUERY_PROFILE_PROJECT_ROOT=/path/to/repository

make clangd-fact-query-profile \
  CLANGD_FACT_QUERY_PROFILE_INDEX_DIR=/path/to/repository/.cache/clangd/index \
  CLANGD_FACT_QUERY_PROFILE_PROJECT_ROOT=/path/to/repository

make clangd-workload-gate \
  CLANGD_WORKLOAD_GATE_INDEX_DIR=/path/to/repository/.cache/clangd/index \
  CLANGD_WORKLOAD_GATE_PROJECT_ROOT=/path/to/repository \
  CLANGD_WORKLOAD_GATE_SUBJECT_ID=fmt-11.2.0
```

Promotion still requires exact semantic parity and at least 20% end-to-end
improvement for the exact capability surface being enabled. SCIP position
queries continue to use the SCIP occurrence index. The C/C++ query index owns
file interval postings and target postings in C++; supported position results
cross Python only as compact locations and target IDs. The full graph is built
only for a named compatibility fallback. Raw query-index objects continue to
advertise graph routes as unavailable because exact roles and output ranges
require the `ClangdGraphDecoder` span adapter; `decode_query_provider()`
composes that adapter with compact adjacency and advertises the graph-free
route capability truthfully.
The broader workload gate keeps every measured arm process-isolated, covers
position-first/route-first/mixed sessions and concurrent route access, and
applies explicit wall-time and peak-RSS budgets. Position-first and route-first
each require at least 20% acceleration and zero graph materializations. The
route arm includes direct symbols and bounded query-only fallback. clangd
generation remains a separately labeled preparation measurement.
