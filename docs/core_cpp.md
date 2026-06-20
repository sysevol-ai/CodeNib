<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Core C++ Backend

The `core/` directory contains a C++ implementation of the high-traffic pieces
of the Python graph pipeline. It mirrors the behaviour of
`codeminer.graph.code_graph.CodeGraph` and the active serial SCIP decoders while
using the libigraph C API for higher throughput on large `.decoded` SCIP index
files.

## Components

- `code_graph.h` / `code_graph.cpp` — graph container (vertices, edges, metadata)
  compatible with the Python implementation.
- `graph_layers.h` / `graph_layers.cpp` — language-agnostic default layer
  classification for CodeGraph edge types. This is shared by SCIP, clangd, and
  generic-LSP graphs after they have normalized into the common schema.
- `scip_decode.h`, `scip_decoder_registry.{h,cpp}`, and the
  language-specific `scip_decode_*.{h,cpp}` files — translate `.decoded` SCIP
  indexes into the C++ `CodeGraph` and keep decoder aliases/factory wiring out
  of bindings and smoke-test CLIs.
- `bindings/pybind_module.cpp` — exposes `decode_scip(...)` and the optional
  `classify_edge_layers(...)` binding. The binding delegates to the core
  `graph_layers` module; it should not own graph algorithms.
- `CMakeLists.txt` — builds the static library `codeminer_core`, suitable for wrapping
  with pybind11 or another binding layer.

## Building

Requirements: CMake >= 3.15, a C++17 compiler, pkg-config, RE2 headers, and
pybind11. The build vendors c-igraph through CMake FetchContent to avoid symbol
clashes with the Python `igraph` wheel.

```bash
make core-system-deps-ubuntu  # Ubuntu system packages
make core-build               # pybind11 + CMake configure/build
make core-test                # C++ smoke + Python/core parity checks
```

The resulting static library and pybind module are placed in `build/core`.
`make core-test` currently validates the C++ executable smoke tests,
`graph_layers`, registry-driven Python and C++ core language metadata, and
serial/core parity for the active accelerated SCIP backends: Python, Go, Rust,
Ruby, TypeScript, and the JavaScript/TypeScript aliases.

Ruby has a dedicated C++ decoder because real `ruby/rake` profiling showed the
serial local decode path was the bottleneck after `scip-ruby` produced a large
text index. On the `ruby/rake` gate, serial `process_index` took 7.58s while the
C++ backend took 1.04s after filtering to `lib/`; both routes produced 815
vertices, 3,466 edges, and no vertex-attribute or edge-multiset differences.

Java, C#, PHP, and Scala are active SCIP cold-start routes but intentionally
remain serial-only in Python. Local profiles show their external indexers
dominate cold-start time: `scip-java` on `jitpack/maven-simple` took about 5.98s
to index while protoc decode took 0.01s, Python graph decode 0.007s, and
range-index construction 0.001s; `scip-dotnet` on the recorded C# fixture took
about 4.3-4.8s while Python decode/build was about 0.01s; the small PHP Composer
gate spent about 0.551s in SCIP indexing, 0.008s in protoc decode, and 0.007s in
Python graph decode; the `sbt/io` Scala gate spent about 74.527s in `scip-java`
indexing, 0.099s in protoc decode, 2.156s in Python graph decode, and 0.069s in
range-index construction. Adding C++ decoder files for those languages is not
justified until a larger profile shows local decode/build time crossing the 20%
acceleration gate.

## Profiling Shared Helpers

The layer-index helper can be profiled independently of SCIP decoding:

```bash
PYTHONPATH=build/core:$PYTHONPATH \
python scripts/profiling/profile_graph_layers.py --edges 1000000 --reps 3
```

Latest local sample:

```json
{
  "core_seconds_min": 0.1028488609008491,
  "edges": 1000000,
  "parity": true,
  "python_seconds_min": 0.3491414119489491,
  "speedup_vs_python": 3.3947037321641997
}
```

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
