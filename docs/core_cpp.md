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
- zlib development headers
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
`decode_scip_fact_buffer(...)`, `fact_batch_buffer_contract(...)`,
`decode_clangd_fact_query_index(...)`, `clangd_fact_query_contract()`,
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

## FactBatchBuffer v1

The optional buffer transport consumes `DecodedRecords` directly and crosses
the pybind boundary as a constant number of fixed-width little-endian tables
plus one shared UTF-8 arena. It can expose provider-neutral per-file semantic
facts, preserve the exact legacy vertex/edge/range projection, or omit graph
compatibility tables for fact-only consumers. Python validates the fixed
envelope immediately, then checks the selected projection's flags, string
references, identities, ranges, and graph endpoints before constructing its
consumer result. Zero-copy exports are read-only and retain their native owner.

The established decode path still defaults to the legacy transport. Set
`CODENIB_CORE_FACT_BUFFER=auto` to try the ownership-safe zero-copy buffer path
with compatible fallback, or `required` to fail closed when its ABI or
materialization fails. Neither the graph-compatible arm nor the eager logical
`FactBatch` arm passed the 20% end-to-end promotion gate recorded in the
internal multi-language roadmap, so this setting is not promoted by default.
Reproduce the alternating-arm measurement with:

```bash
make fact-buffer-profile \
  FACT_BUFFER_PROFILE_INDEX=/path/to/index.decoded \
  FACT_BUFFER_PROFILE_LANGUAGE=python \
  FACT_BUFFER_PROFILE_PROJECT_ROOT=/path/to/repository \
  FACT_BUFFER_PROFILE_OUTPUT=/tmp/fact-buffer-report.json \
  FACT_BUFFER_PROFILE_EXTRA_ARGS='--iterations 7 --warmups 2 --include-semantic-consumer'
```

## FactQueryIndex v1

`SCIPDecoderCore.decode_query_index()` can stop at a graph-free native index
for symbol definition and reference consumers. The index owns the decoded
records and integer postings, resolves canonical, display, and bare names, and
returns only fully anchored references. Its capability metadata explicitly
marks position and route queries unavailable. Invalid endpoints, definition
ranges, duplicate names, and unanchored references fail closed before any
public result is returned.

This API is separate from `decode()`, whose graph behavior is unchanged.
`CODENIB_NATIVE_FACT_QUERY_INDEX=auto` selects native indexing only for Python
and Rust; other languages receive the complete compatible graph. Use `off` to
force that graph or `required` to attempt the native path without fallback.
The promotion gate starts from an existing `index.decoded` artifact and
measures both decode-to-query-ready startup and an identical symbol workload.
Reproduce it with:

```bash
make fact-query-profile \
  FACT_QUERY_PROFILE_INDEX=/path/to/index.decoded \
  FACT_QUERY_PROFILE_LANGUAGE=python \
  FACT_QUERY_PROFILE_PROJECT_ROOT=/path/to/repository \
  FACT_QUERY_PROFILE_OUTPUT=/tmp/fact-query-report.json \
  FACT_QUERY_PROFILE_EXTRA_ARGS='--iterations 15 --warmups 5'
```

Pass `--external-index-seconds` through `FACT_QUERY_PROFILE_EXTRA_ARGS` when a
separate cold-start analysis should include unchanged SCIP generation time.

## Native clangd Symbol Queries

The C/C++ query-specific path starts from an existing project-local clangd
`.idx` directory. `decode_clangd_fact_query_index(...)` reads direct shards in
stable filename order and decodes RIFF string, symbol, reference, and relation
rows directly into provider-neutral `DecodedRecords`. `FactQueryIndex` then
builds integer postings without a `CodeGraph`, igraph, or intermediate Python
record dictionaries. Its explicit contract advertises symbol definition and
reference support while position and route capabilities remain false.

Call `LSIndexer.process_query_index()` to obtain the capability-specific index,
or `process_query_provider()` for a hybrid provider. Successful symbol queries
stay graph-free. The first position or route request lazily materializes the
complete compatible graph, and later unsupported requests reuse it. The normal
`process_index()` path, persistence format, incremental checks, range indexes,
and graph quality behavior are unchanged.

`CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX=auto` is promoted by default after the
persisted-artifact query-ready gate passed on both the generated C++ fixture
and `fmt`. It falls back to the complete graph on a native candidate failure;
`off` always selects that graph and `required` fails closed. Reproduce the
gate with:

```bash
make clangd-fact-query-profile \
  CLANGD_FACT_QUERY_PROFILE_INDEX_DIR=/path/to/.cache/clangd/index \
  CLANGD_FACT_QUERY_PROFILE_PROJECT_ROOT=/path/to/repository \
  CLANGD_FACT_QUERY_PROFILE_OUTPUT=/tmp/clangd-fact-query.json \
  CLANGD_FACT_QUERY_PROFILE_EXTRA_ARGS='--iterations 15 --warmups 5'
```

This result measures an already generated `.idx` directory through identical
definition/reference work. It does not claim faster clangd generation. Native
position/route lookup, serving integration, content receipts, and systematic
format-version/resource hardening remain independent follow-up gates.

## Verify

```bash
make core-test
```

This runs the C++ smoke tests, graph-layer checks, registry consistency checks,
Fact transport/query tests, native clangd result/error and fallback tests, and
the serial/core parity fixtures available in the checkout. Before pytest it
also requires the built extension to export the native clangd decode and
contract bindings, so an absent or stale extension cannot turn that gate into
a skip. Some integration-cache parity cases are skipped when their generated
SCIP fixtures are not present, so a successful local run should be read
together with its skip report.

## Components

- `code_graph.{h,cpp}` implements the C++ graph container.
- `decoded_records.h` defines the provider-neutral pre-graph boundary.
- `fact_batch_buffer.{h,cpp}` defines the v1 native buffer ABI and encoder.
- `fact_query_index.{h,cpp}` implements graph-free symbol and reference
  postings.
- `clangd_fact_query.{h,cpp}` decodes clangd RIFF shards into provider-neutral
  records for the query-specific path.
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
