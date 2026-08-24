<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib Core (C++)

This directory contains the optional C++ implementation of selected
high-traffic graph operations and SCIP decoders. It preserves the Python
`codenib.graph.code_graph.CodeGraph` contract; languages without an accepted
core decoder continue to use the serial Python path.

## Components

- `code_graph.{h,cpp}` — C++ graph container.
- `decoded_records.h` — provider-neutral rows before graph materialization.
- `fact_batch_buffer.{h,cpp}` — versioned flat semantic and graph-compatibility
  tables for the native/Python boundary.
- `clangd_fact_query.{h,cpp}` — deterministic clangd RIFF shard decoding into
  provider-neutral records for graph-free symbol, position, and route queries.
- `content_digest.{h,cpp}` — dependency-free streaming SHA-256 for native
  content receipts.
- `graph_layers.{h,cpp}` — shared normalized edge-layer classification.
- `scip_decode_base.{h,cpp}` — common loading, document scheduling, merge, and
  post-processing behavior.
- `scip_decode_common.{h,cpp}` — language-neutral SCIP parsing and subgraph
  helpers.
- `scip_decode_<language>.{h,cpp}` — language-specific decoder policy.
- `scip_decoder_registry.{h,cpp}` — canonical decoder names and aliases.
- `bindings/pybind_module.cpp` — Python bindings for `decode_scip(...)`,
  `decode_scip_fact_buffer(...)`, clangd decode/receipt APIs,
  `classify_edge_layers(...)`, and registry
  inspection.

`codenib_core.decode_scip(...)` is a low-level flat transport API. It does not
apply source-aware post-decode layers, including TypeScript import enrichment,
and must not be consumed as a complete persisted `CodeGraph`. Supported
application entry points route through `SCIPDecoderCore` or `LSIndexer`, which
apply the shared enrichment path used for serial/core parity.

## Build And Test

Requirements:

- CMake 3.15 or newer
- a C++17 compiler
- `pkg-config`
- RE2 development headers
- zlib development headers
- pybind11 in the active Python environment

From the repository root:

```bash
make core-system-deps-ubuntu  # Ubuntu only
make core-build
make core-test
```

The resulting library and Python extension are placed in `build/core`.
c-igraph is fetched by CMake and linked privately to avoid symbol clashes with
the Python `igraph` wheel.

`make core-test` is also the maintained native gate used by trusted full CI.
It runs every C++ test executable, verifies that the built extension exports
the required clangd query ABI, and then runs the SCIP, Fact, lazy SCIP provider,
consumer-profiler, clangd, and profiling-contract Python tests with
`build/core` first on `PYTHONPATH`.

Some parity tests use generated SCIP integration fixtures and are skipped when
those caches are absent. Always inspect the skip report from `make core-test`.

## Pre-Graph Decode Boundary

`SCIPDecoderBase` first produces `DecodedRecords`. The established `decode()`
path materializes those rows into the same `CodeGraph`; `decode_records()` lets
future capability-specific consumers stop before igraph. The record merge owns
the established first-definition and edge-deduplication policy, and
language-specific postprocessing runs before either consumer observes rows.

## FactBatchBuffer v1

`decode_scip_fact_buffer(...)` encodes `DecodedRecords` into fixed-width,
little-endian tables plus one shared UTF-8 arena. Consumers may request the
provider-neutral per-file `FactBatch` projection, the exact legacy graph
projection, or omit the graph tables entirely. The Python view validates the
fixed envelope immediately and the complete selected projection before
returning a materialized consumer result. The zero-copy mode keeps native
storage alive through read-only buffer owners.

The graph-compatible runtime remains experimental and defaults to the legacy
transport because the candidate did not clear the 20% end-to-end promotion
gate. `CODENIB_CORE_FACT_BUFFER=auto` enables the ownership-safe zero-copy
candidate with compatible fallback; `required` fails closed for ABI and parity
testing. Reproduce both graph and eager logical-fact arms with:

```bash
make fact-buffer-profile \
  FACT_BUFFER_PROFILE_INDEX=/path/to/index.decoded \
  FACT_BUFFER_PROFILE_LANGUAGE=python \
  FACT_BUFFER_PROFILE_EXTRA_ARGS='--include-semantic-consumer'
```

## FactQueryIndex v1

`decode_scip_fact_query_index(...)` owns immutable `DecodedRecords` and builds
integer symbol/reference postings without constructing `CodeGraph` or igraph.
Its v1 capabilities are intentionally limited to canonical/display/bare symbol
resolution, definition metadata, and anchored incoming references. Position
and route queries report unsupported; a malformed endpoint, definition range,
duplicate name, or unanchored reference rejects the whole candidate.

The existing `SCIPDecoderCore.decode()` behavior does not change. Call
`decode_query_index()` for this capability-specific route. Its
`CODENIB_NATIVE_FACT_QUERY_INDEX=auto` default selects the native index only
for Python and Rust, which passed the 20% query-ready gate; `off` always
returns the complete graph and `required` attempts native indexing for any
core language without fallback. Reproduce the gate with:

```bash
make fact-query-profile \
  FACT_QUERY_PROFILE_INDEX=/path/to/index.decoded \
  FACT_QUERY_PROFILE_LANGUAGE=python \
  FACT_QUERY_PROFILE_OUTPUT=/tmp/fact-query-report.json
```

The low-level decode payload also exposes an `input_receipt` for the exact
`index.decoded` bytes consumed by C++. Rust receipts additionally enumerate
the exact root/member `Cargo.toml` inputs and the resulting internal-crate
set. `FactQueryIndex.prove_filter_identity(allowed_files,
expected_query_surface_sha256)` requires canonical, UTF-8 bytewise-sorted
unique paths and performs a read-only O(F+V+E) scan over the file set and
immutable records: every structural path, definition, and reference anchor
must already belong to the supplied repository surface. It never deletes or
renumbers a row, so a proof cannot change ambiguity resolution, ordering, or
`top_k` behavior. The required digest comes from the trusted serial-writer
receipt; the proof independently returns its order-sensitive digest over every
immutable vertex and edge so both surfaces must agree on identity, fields, and
insertion order rather than counts alone.

Consumer code should use
`codenib.scip_interface.scip_query.load_fact_query_candidate(...)`, not the
raw binding. That graph-free facade binds the native receipt and filter proof
to a current single-language compiler manifest, source fingerprint, builder
and filter policy, resolved project root, persisted graph-writer receipt, and
query-surface digest. Receipt capture is an explicit compiler-build opt-in;
ordinary `run_pipeline()` callers retain the default path without the extra
artifact scans. Incremental, partial, multi-language, source-coverage fallback,
mutated, or otherwise unproven inputs reject the whole candidate. A
reference-only external target is retained only when it is part of the exact
serial filtered query-surface digest and every incoming reference has an
allowed source anchor; it is never inferred from the native index alone. The
first candidate contract admits Python and Rust only. This admission layer
does not enable an MCP route or make an end-to-end performance claim.

### SCIP MCP Consumer Experiment

`load_scip_query_provider()` keeps admitted symbol definition/reference calls
on `FactQueryIndex` and atomically lazy-loads the bound persisted graph for
position and route calls. Invalid shapes do not load the graph, concurrent
first fallback publishes one complete provider, later symbol calls stay
native, and snapshot or loader failures are terminal. Canonically normalized
public MCP JSON remains identical to the persisted-graph provider; physical
routing is diagnostic-only. The gate compares complete tool-result payloads,
not raw JSON-RPC transport envelopes.

The experimental selector is not connected to production `ServerContext`.
`CODENIB_SCIP_FACT_QUERY_PROVIDER` defaults to `off`; `auto` is limited to a
separate consumer-promoted language set and `required` fails closed. The fixed
Python and Rust consumer gates preserved exact behavior and all safety gates,
but both missed the required 20% p50 and p95 improvement. The promoted set
therefore remains empty and production routing is unchanged. Reproduce one
fixed subject at a time with:

```bash
make scip-mcp-consumer-gate \
  SCIP_MCP_CONSUMER_GATE_MANIFEST=/path/to/repo_manifest.json \
  SCIP_MCP_CONSUMER_GATE_PROJECT_ROOT=/path/to/clean/checkout \
  SCIP_MCP_CONSUMER_GATE_SUBJECT_ID=python-codenib \
  SCIP_MCP_CONSUMER_GATE_OUTPUT=/tmp/scip-mcp-python.json
```

Use `rust-ruff` and the corresponding Ruff paths for the Rust run. The durable
multi-language roadmap records exact timings, revisions, and receipts.

## Native clangd Symbol, Position, And Route Queries

For C and C++ projects with an existing project-local clangd index,
`decode_clangd_fact_query_index(...)` reads the direct `*.idx` children in
stable filename order, decodes them into `DecodedRecords`, and builds the same
`FactQueryIndex` without constructing `CodeGraph`, igraph, or Python record
dictionaries. The initial symbol-only index covers definition and reference
lookup by symbol plus complete compact route adjacency and the legacy vertex
traversal order. Each decode also exposes a content-bound snapshot over the
query contract, normalized project root, exact supported RIFF versions, sorted
shard names, and the exact bytes consumed by the decoder. clangd relation rows
may be unanchored, so this provider explicitly opts into that record policy
while still rejecting partial or invalid anchors.

Use `LSIndexer.process_query_index()` for the capability-specific index or
`process_query_provider()` for hybrid behavior. The provider keeps successful
symbol queries on the native index. Its first exact position request lazily
builds a second native occurrence view; a successful definition/reference
position lookup never constructs `CodeGraph` or igraph. The view stores
provider-neutral zero-based, half-open file ranges, role bits, and optional
target/container vertex ids in C++, then builds per-file interval postings and
per-target postings without Python record dictionaries. Unsupported,
ambiguous, declaration-only, unanchored, or missing-source positions fall back
with a stable reason.

The hybrid provider adapts the compact adjacency to the established route
contract as `native-clangd-route-adjacency-v1`. Direct-symbol routes traverse
the native postings; query-only routes use a deterministic bounded scan and
candidate cache. Source spans are enriched lazily only for nodes touched by the
route. A successful route never constructs `CodeGraph` or igraph. Incomplete
adjacency, unavailable support, or any native route error falls back to one
complete compatible graph and recomputes the whole request; a partial native
route is never returned. Existing `process_index()`, graph persistence,
incremental processing, and quality gates are unchanged.

The clangd query ABI is `clangd-riff-fact-query-v3`. Position offsets are
explicit and receipt-bound: UTF-16 is the default, while UTF-8 and UTF-32 are
accepted at the provider boundary. Full and incremental background indexing
use the same normalized clangd offset flag, so their rows cannot silently mix
coordinate systems.

`CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX=auto` is the default and falls back to
the compatible graph if native decoding fails. Set it to `off` to force the
graph or `required` to fail closed. Reproduce the alternating query-ready gate
from an already generated `.idx` directory with:

```bash
make clangd-fact-query-profile \
  CLANGD_FACT_QUERY_PROFILE_INDEX_DIR=/path/to/.cache/clangd/index \
  CLANGD_FACT_QUERY_PROFILE_PROJECT_ROOT=/path/to/repository \
  CLANGD_FACT_QUERY_PROFILE_OUTPUT=/tmp/clangd-fact-query.json \
  CLANGD_FACT_QUERY_PROFILE_EXTRA_ARGS='--iterations 15 --warmups 5'
```

The first receipt hash shares the decoder's existing byte buffers. A second
canonical read before publication detects mutation during decode. The hybrid
provider verifies the receipt before every native route and before and after
lazy graph record collection. A mismatch fails the provider session rather
than combining index generations; restart it to adopt new shards. Both receipt
stages are included in the profiling gate.

The mixed-workload gate requires at least 20% acceleration for symbol-only,
position-first, and route-first sessions, exact public-result parity, and zero
native graph materializations in every workload. Mixed sessions retain the 20%
non-regression budget. Concurrent native routes must also remain deterministic
and graph-free. This result makes no claim about clangd index generation time.

## Use Through Python

Application code should select the core decoder through `LSIndexer`, which
also builds occurrence and range indexes and persists the normal graph format:

```python
from codenib.ls_router import LSIndexer

indexer = LSIndexer(
    project_root="/path/to/repository",
    language="python",
    decoder_backend="core",
)
graph = indexer.run_pipeline()
```

The accepted language set comes from `codenib.languages`; the C++ registry and
Python registry must remain in parity. See the
[Optional C++ Core](https://docs.codenib.ai/core_cpp/) reference for the
current support set and contributor contract.
