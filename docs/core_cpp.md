<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Optional C++ Core

The optional `core/` module accelerates selected graph operations while
preserving the Python `CodeGraph` contract. CodeNib continues to work without
it; unsupported languages and installations without the extension use the
serial Python implementation.

The accepted C++ SCIP decoders are currently:

- Python
- Go
- Rust
- Ruby
- TypeScript, including the `ts` and `js` aliases

Java, C#, Kotlin, PHP, and Scala currently use their serial Python decoders.
The generated [Language Capabilities](language_capabilities.md) matrix is the
source of truth for this support set.

## Build

Requirements:

- CMake 3.15 or newer
- a C++17 compiler
- `pkg-config`
- RE2 development headers
- pybind11 in the active Python environment

On Ubuntu:

```bash
make core-system-deps-ubuntu
make core-build
```

The build places the extension in `build/core`. Add that directory to
`PYTHONPATH` when running directly from a source checkout:

```bash
export PYTHONPATH="$PWD/build/core:$PYTHONPATH"
```

The build vendors c-igraph through CMake FetchContent and links it privately to
avoid symbol clashes with the Python `igraph` wheel.

## Use The Accelerated Decoder

Select the backend through the normal `LSIndexer` API:

```python
from codenib.ls_router import LSIndexer

indexer = LSIndexer(
    project_root="/path/to/repository",
    language="python",
    decoder_backend="core",
)
graph = indexer.run_pipeline(skip_level=None)
```

Use `skip_level=None` when explicitly comparing decoders; `"graph"` may reuse a
graph written by an earlier serial run. If the extension is unavailable or the
language has no accepted C++ decoder, the pipeline logs the decoding failure
and returns no graph. Non-SCIP backends such as C/C++ ignore
`decoder_backend` because they do not use a SCIP decoder.

The pybind module also exposes lower-level `decode_scip(...)`,
`classify_edge_layers(...)`, and decoder-registry inspection functions. These
are primarily integration surfaces; application code should normally use
`LSIndexer` so filtering, occurrence indexes, range indexes, and persistence
remain consistent with the serial path.

## Pre-Graph Decode Boundary

The C++ decoder now merges each SCIP index into provider-neutral
`DecodedRecords` before constructing igraph. The normal `decode()` API then
materializes the same graph, so persisted schema and public graph behavior do
not change. `decode_records()` is the reusable boundary for later consumers:
it owns deterministic vertex order, indexed edges, project identity, and
language-specific postprocessing without constructing a `CodeGraph`.

## Verify

```bash
make core-test
```

This runs the C++ smoke tests, graph-layer checks, registry consistency checks,
and the serial/core parity fixtures available in the checkout. Some
integration-cache parity cases are skipped when their generated SCIP fixtures
are not present, so a successful local run should be read together with its
skip report.

## Components

- `code_graph.{h,cpp}` implements the C++ graph container.
- `decoded_records.h` defines the provider-neutral pre-graph boundary.
- `graph_layers.{h,cpp}` classifies normalized edge types into reusable graph
  layers.
- `scip_decode_base.{h,cpp}` and `scip_decode_common.{h,cpp}` provide shared
  decoder mechanics.
- `scip_decode_<language>.{h,cpp}` owns language-specific symbol and metadata
  policy.
- `scip_decoder_registry.{h,cpp}` owns canonical decoder names and aliases.
- `bindings/pybind_module.cpp` exposes the extension to Python.

## Contributor Contract

Add a C++ decoder only when profiling shows decode/build is a meaningful
bottleneck. A new decoder must:

1. reuse `SCIPDecoderBase` for loading, parallel document work, merge order,
   and post-processing;
2. create nodes and edges through `SubgraphBuilder`;
3. keep language policy in its language-specific decoder;
4. update the C++ registry and Python language registry together;
5. pass serial/core node, attribute, and edge-multiset parity checks.

Performance measurements and promotion decisions belong in versioned benchmark
artifacts or internal engineering records, not in this user-facing reference.
