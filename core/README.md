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
  provider-neutral records for graph-free symbol queries.
- `graph_layers.{h,cpp}` — shared normalized edge-layer classification.
- `scip_decode_base.{h,cpp}` — common loading, document scheduling, merge, and
  post-processing behavior.
- `scip_decode_common.{h,cpp}` — language-neutral SCIP parsing and subgraph
  helpers.
- `scip_decode_<language>.{h,cpp}` — language-specific decoder policy.
- `scip_decoder_registry.{h,cpp}` — canonical decoder names and aliases.
- `bindings/pybind_module.cpp` — Python bindings for `decode_scip(...)`,
  `decode_scip_fact_buffer(...)`, `classify_edge_layers(...)`, and registry
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
the required clangd query ABI, and then runs the SCIP, Fact, clangd, and
profiling-contract Python tests with `build/core` first on `PYTHONPATH`.

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

## Native clangd Symbol Queries

For C and C++ projects with an existing project-local clangd index,
`decode_clangd_fact_query_index(...)` reads the direct `*.idx` children in
stable filename order, decodes them into `DecodedRecords`, and builds the same
`FactQueryIndex` without constructing `CodeGraph`, igraph, or Python record
dictionaries. The v1 slice covers definition and reference lookup by symbol.
clangd relation rows may be unanchored, so this provider explicitly opts into
that record policy while still rejecting partial or invalid anchors.

Use `LSIndexer.process_query_index()` for the capability-specific index or
`process_query_provider()` for hybrid behavior. The provider keeps successful
symbol queries on the native index and lazily constructs the complete graph
once when a position or route query is requested. Existing `process_index()`,
graph persistence, incremental processing, and quality gates are unchanged.

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

This baseline makes no claim about clangd index generation time. Native
position and route queries, serving integration, content receipts, and a
hardened clangd artifact compatibility/resource contract remain separate
promotion gates.

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
