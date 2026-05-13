<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Codeminer Core (C++)

This directory contains a C++ implementation of the high-traffic pieces of the Python `codeminer` graph pipeline. The goal is to mirror the behaviour of `codeminer.graph.code_graph.CodeGraph` and `codeminer.scip_interface.scip_decode.SCIPGraphDecoder` while leveraging the libigraph C API for better throughput on large `.decoded` index files.

## Components

- `code_graph.h` / `code_graph.cpp` – graph container that stores vertices, edges, and metadata compatible with the Python implementation.
- `scip_decode.h` / `scip_decode.cpp` – translates a `.decoded` SCIP index into the C++ `CodeGraph`.
- `CMakeLists.txt` – builds a static library (`codeminer_core`) suitable for later wrapping with pybind11 or another binding layer.

## Building

Requirements:

- CMake ≥ 3.15
- A compiler with C++17 support
- libigraph and pkg-config installed on the system

Example build commands (from the repository root):

```bash
cmake -S core -B build/core
cmake --build build/core
```

The resulting library (`libcodeminer_core.a` or `.lib`) will be placed in `build/core`.

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

The `save_graph` helper currently writes a JSON snapshot for inspection or downstream loading. A dedicated `load_graph` implementation and Python bindings can be layered on afterward.
