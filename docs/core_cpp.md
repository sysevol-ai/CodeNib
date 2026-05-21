<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Core C++ Backend

The `core/` directory contains a C++ implementation of the high-traffic pieces of the
Python graph pipeline. It mirrors the behaviour of
`codeminer.graph.code_graph.CodeGraph` and
`codeminer.scip_interface.scip_decode.SCIPGraphDecoder` while using the libigraph C API
for higher throughput on large `.decoded` SCIP index files.

## Components

- `code_graph.h` / `code_graph.cpp` — graph container (vertices, edges, metadata)
  compatible with the Python implementation.
- `scip_decode.h` / `scip_decode.cpp` — translates a `.decoded` SCIP index into the C++
  `CodeGraph`.
- `CMakeLists.txt` — builds the static library `codeminer_core`, suitable for wrapping
  with pybind11 or another binding layer.

## Building

Requirements: CMake ≥ 3.15, a C++17 compiler, and libigraph + pkg-config installed.

```bash
cmake -S core -B build/core
cmake --build build/core
```

The resulting library (`libcodeminer_core.a`) is placed in `build/core`.

## Using the decoder

```cpp
#include "scip_decode.h"

int main() {
    codeminer::core::SCIPGraphDecoder decoder("index.decoded", ".");
    auto graph = decoder.decode();
    graph.save_graph("graph.json");  // JSON snapshot for inspection / downstream load
    return 0;
}
```

See [`core/README.md`](https://github.com/sysevol-ai/CodeMiner/blob/main/core/README.md)
for the latest details.
