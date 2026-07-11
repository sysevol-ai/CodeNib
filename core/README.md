<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Codeminer Core (C++)

This directory contains a C++ implementation of the high-traffic pieces of the
Python `codeminer` graph pipeline. The goal is to mirror the behaviour of
`codeminer.graph.code_graph.CodeGraph` and the active serial SCIP decoders while
leveraging the libigraph C API for better throughput on large `.decoded` index
files.

## Components

- `code_graph.h` / `code_graph.cpp` – graph container that stores vertices, edges, and metadata compatible with the Python implementation.
- `graph_layers.h` / `graph_layers.cpp` – shared graph-layer classification
  used by Python graph indexing when the pybind module is available.
- `scip_decode_common.h` / `scip_decode_common.cpp` – shared SCIP decoder
  subgraph builders and language-neutral text/string parsing helpers.
- `scip_decode.h`, `scip_decode_base.{h,cpp}`,
  `scip_decoder_registry.{h,cpp}`, and the language-specific
  `scip_decode_*.{h,cpp}` files – translate `.decoded` SCIP indexes into the
  C++ `CodeGraph` and keep core decoder aliases/factory wiring centralized.
- `bindings/pybind_module.cpp` – exposes `decode_scip(...)` and
  `classify_edge_layers(...)`; binding code should delegate algorithms to core
  modules.
- `CMakeLists.txt` – builds a static library (`codeminer_core`) suitable for later wrapping with pybind11 or another binding layer.

## Building

Requirements:

- CMake ≥ 3.15
- A compiler with C++17 support
- pkg-config and RE2 headers installed on the system
- pybind11 in the active Python environment

c-igraph is vendored through CMake FetchContent and linked privately into the
pybind module to avoid symbol clashes with the Python `igraph` wheel.

Example build commands (from the repository root):

```bash
make core-build
make core-test
```

The resulting library and Python extension are placed in `build/core`.

## Decoder Engineering Contract

Add C++ decoder code only after profiling shows local decode/build is a real
bottleneck, not just because a language has an active SCIP cold-start route.
New decoders should:

- Reuse `SCIPDecoderBase` for file loading, document extraction, parallel
  document processing, merge order, and post-processing hooks.
- Use `SubgraphBuilder` for node/edge construction instead of writing directly
  to `CodeGraph` from language parsers.
- Put only language-neutral SCIP text helpers in `scip_decode_common.h` /
  `scip_decode_common.cpp`. Language policy, metadata loading, and symbol
  normalization stay in `scip_decode_<language>.cpp`.
- Register canonical language names and aliases in `scip_decoder_registry.cpp`
  and keep the Python registry parity tests green before advertising a decoder
  as accepted.
- Prove serial/core parity for the relevant fixture before recording a speedup
  in docs.

## Using the Decoder

```cpp
#include "scip_decode.h"

int main() {
    codeminer::core::SCIPGraphDecoder decoder("index.decoded", ".");
    auto graph = decoder.decode();
    graph.save_graph("graph.json");
    return 0;
}
```

The `save_graph` helper currently writes a JSON snapshot for inspection or
downstream loading. The Python pybind wrapper converts `decode_scip(...)` output
into `CodeGraph` and keeps serial/core parity tests as the compatibility gate.
