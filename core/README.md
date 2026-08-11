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
