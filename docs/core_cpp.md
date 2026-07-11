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
- `scip_decode_common.h` / `scip_decode_common.cpp` — shared per-document
  `SubgraphBuilder` utilities and language-neutral SCIP text/string helpers
  used by the accelerated decoders.
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
The pybind module also exposes the C++ decoder registry so tests can compare it
directly with `codeminer.languages.core_decoder_languages(...)`.

Ruby has a dedicated C++ decoder because real `ruby/rake` profiling showed the
serial local decode path was the bottleneck after `scip-ruby` produced a large
text index. On the `ruby/rake` gate, serial `process_index` took 7.58s while the
C++ backend took 1.04s after filtering to `lib/`; both routes produced 815
vertices, 3,466 edges, and no vertex-attribute or edge-multiset differences.

Java, C#, Kotlin, PHP, and Scala are active SCIP cold-start routes but
intentionally remain serial-only in Python. Local profiles show their external
indexers dominate cold-start time: `scip-java` on `jitpack/maven-simple` took
about 5.98s to index while protoc decode took 0.01s, Python graph decode
0.007s, and range-index construction 0.001s; `scip-dotnet` on the recorded C#
fixture took about 4.3-4.8s while Python decode/build was about 0.01s; the
KotlinPoet 2.2.0 gate spent 61.914s in `scip-java`, 0.150s in protoc decode,
6.931s in Python graph decode, and 0.008s in range-index construction; the
small PHP Composer gate spent about 0.551s in SCIP indexing, 0.008s in protoc
decode, and 0.007s in Python graph decode; the `sbt/io` Scala gate spent about
74.527s in `scip-java` indexing, 0.099s in protoc decode, 2.156s in Python
graph decode, and 0.069s in range-index construction. Adding C++ decoder files
for those languages is not justified until a larger profile shows local
decode/build time crossing the 20% acceleration gate.

## Decoder Engineering Contract

The accepted C++ decoder set is deliberately smaller than the active SCIP
cold-start set. A new language should be added to `core/` only after the
profiling gate above is met and the implementation follows these boundaries:

- Start from `SCIPDecoderBase` for file loading, document extraction, parallel
  worker execution, merge ordering, and post-processing hooks.
- Build nodes and edges through `SubgraphBuilder`; language decoders should not
  write directly to the graph container.
- Put language-neutral SCIP text primitives in `scip_decode_common.h` /
  `scip_decode_common.cpp`. Current shared helpers cover integer extraction,
  whitespace splitting, suffix checks, trailing-character stripping, and
  backtick removal.
- Keep language policy in the owning decoder file: metadata discovery,
  standard-library filtering, symbol normalization, scope rules, and display
  naming are not generic helpers.
- Update `scip_decoder_registry.cpp`, Python registry metadata, and
  serial/core parity tests together before documenting the language as
  core-accelerated.

Large-repo acceleration decisions should use the manifest-driven harness before
new decoder work starts:

```bash
# Inspect the selected large-repo targets without cloning or indexing.
make large-scip-profile LARGE_SCIP_PROFILE_EXTRA_ARGS="--dry-run"

# Profile one serial-only active language on the checked-in large-repo targets.
make large-scip-profile LARGE_SCIP_PROFILE_EXTRA_ARGS="--language java"
```

The default manifest is `scripts/profiling/large_scip_repos.yml`. Results are
written to `LARGE_SCIP_PROFILE_OUTPUT_DIR` as `large_scip_profile.json` and
`large_scip_profile.md`. Each row reports external index time, protoc decode
time, serial graph decode/build time, optional core decode time for accepted
C++ languages, and whether the 20% local decode/build gate is crossed.

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
