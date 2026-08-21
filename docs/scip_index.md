<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# SCIP And Graph Indexing

CodeNib builds a common `CodeGraph` from language-specific indexing backends.
Most supported graph languages use [SCIP](https://github.com/sourcegraph/scip);
C and C++ use clangd indexes. Backend selection comes from CodeNib's central
language registry, so callers should select a language rather than invoke an
indexer implementation directly.

The [Language Capabilities](language_capabilities.md) page is generated from
that registry and is the source of truth for the currently enabled backends.
An enabled backend still requires its external executable to be installed on
the machine running CodeNib.

## Build A Graph Index

Install the graph dependencies, check the target repository, and then select
the graph preset:

```bash
python -m pip install "codenib[graph]==0.2.2"
codenib toolchain install /path/to/repository --scope graph
codenib doctor /path/to/repository --require graph
codenib index /path/to/repository --preset graph
```

`codenib toolchain` detects the repository languages, installs pinned
package-managed providers under `~/.codenib/toolchains`, and reports system or
project-local prerequisites it cannot safely install. `codenib doctor` then
checks the complete build route. Use `--language` when detection is ambiguous:

```bash
codenib doctor /path/to/repository \
  --language python --language typescript --require graph

codenib index /path/to/repository \
  --language python --language typescript --preset graph
```

The CLI stores manifests and reusable indexes below
`$CODENIB_HOME/repositories` (by default `~/.codenib/repositories`), not in the
target checkout. Some language indexers still need to resolve project
dependencies and may create normal toolchain files such as `node_modules` or a
project-local bundle.

## Language Provider Map

Cold-start graph construction and live LSP navigation are separate provider
surfaces. A static graph can serve LSP-shaped definition and reference requests
without launching the live server after construction.

| Language | Graph construction | Optional live LSP |
|---|---|---|
| Python | `scip-python` | `basedpyright-langserver` |
| JavaScript / TypeScript | `scip-typescript` | `typescript-language-server` |
| Go | `scip-go` | `gopls` |
| Rust | `rust-analyzer scip` | `rust-analyzer` |
| C / C++ | clangd index | `clangd` |
| Java | `scip-java` | Eclipse JDT LS |
| Kotlin | `scip-java` | Kotlin language server |
| Scala | `scip-java` | not registered |
| C# | `scip-dotnet` | `csharp-ls` |
| Ruby | `scip-ruby`, with Ruby LSP fallback | `ruby-lsp` |
| PHP | project-local `scip-php`, with Intelephense fallback | `intelephense` |

The manager handles pinned npm, Go, Rustup, .NET tool, and RubyGem providers
when their host runtime is present. OS packages such as clangd, JDKs, and build
tools remain explicit. Preview the exact operations without writing anything:

```bash
codenib toolchain install . --scope all --dry-run
```

Rust graph construction uses the managed Rustup toolchain by default. For a
repository-specific upstream analyzer crash, an operator may temporarily set
`CODENIB_RUST_ANALYZER=/absolute/path/to/rust-analyzer`; both static SCIP
construction and live Rust LSP then use that exact executable. Keep the
override explicit and validate the resulting graph before publication rather
than silently changing the project-wide pin.

## Install Local Toolchains From Source

For a CodeNib source checkout, the Makefile retains broad maintainer and CI
bootstrap targets. End users should prefer `codenib toolchain`; source
developers can install every smoke-test provider below
`$CODENIB_SCIP_TOOLS_DIR`:

```bash
# Ubuntu system packages, Python development dependencies, and all toolchains
make bootstrap-ubuntu

# Same repository-managed tools without sudo/apt
make bootstrap

# Verify every managed command
make toolchain-doctor
```

`make bootstrap` prints the environment variables and `PATH` entry for the
managed tool directory. Use the narrower `make scip` target when only the
primary SCIP/clangd tool set is needed.

CodeNib currently installs the official pinned `@sourcegraph/scip-python`
package. Python path exclusions are applied through a temporary
`pyrightconfig.json`; no custom `scip-python` fork or repository submodule is
required.

Language-specific project preparation is exposed through Makefile targets
where needed:

```bash
make ruby-project-bundle PROJECT_ROOT=/path/to/ruby/repo
make php-project-scip-tool PROJECT_ROOT=/path/to/php/repo
```

For C/C++, provide a `compile_commands.json` when possible. CMake can generate
one directly:

```bash
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

For Make/Autotools projects, `bear -- make` can capture the compilation
database.

## Python API

`LSIndexer` is the low-level API behind graph construction. Always pass the
language explicitly when using it outside the CLI:

```python
from codenib.ls_router import LSIndexer

indexer = LSIndexer(
    project_root="/path/to/repository",
    language="python",
    output_dir="/path/to/cache",
    exclude_patterns=["tests/*", "build/*"],
)

graph = indexer.run_pipeline(skip_level="graph")
if graph is None:
    raise RuntimeError("graph indexing failed")
```

The persisted graph is a binary CodeNib graph, conventionally named
`graph.pkl`; the filename extension does not change its format. A SCIP-backed
cache may also contain:

```text
index.scip       raw SCIP protobuf
index.decoded    decoded protobuf text
graph.pkl        serialized CodeGraph
lsp_index.pkl    occurrence index used by LSP-shaped queries
```

Cache reuse is controlled by `skip_level`:

| Value | Reuse when present | Work that still runs |
| --- | --- | --- |
| `None` | nothing | index, decode, and graph construction |
| `"raw"` | `index.scip` | decode and graph construction |
| `"decode"` | `index.decoded` | graph construction |
| `"graph"` | `graph.pkl` | load the persisted graph |

`clear_cache()` uses the same stage names as preservation boundaries:

```python
indexer.clear_cache("decode")  # keep raw + decoded; remove graph artifacts
indexer.clear_cache("raw")     # keep raw; remove decoded + graph artifacts
indexer.clear_cache("all")     # remove all pipeline artifacts
```

`clear_cache("graph")` keeps all stages and therefore removes nothing.

## Decode An Existing Index

Use `LSGraphDecoder` only when an index was generated separately:

```python
from codenib.ls_router import LSGraphDecoder

decoder = LSGraphDecoder(
    "index.decoded",
    project_root="/path/to/repository",
    language="rust",
)
graph = decoder.decode()
graph.build_range_indexes()
```

C/C++ consumes a clangd index directory rather than a decoded SCIP file:

```python
decoder = LSGraphDecoder(
    ".cache/clangd/index",
    project_root="/path/to/repository",
    language="cpp",
)
```

## Troubleshooting

Start with the repository-aware doctor:

```bash
codenib doctor /path/to/repository --require graph
```

If a Node-based SCIP indexer runs out of memory, raise its heap limit for that
process:

```bash
export NODE_OPTIONS="--max-old-space-size=16384"
```

For a clean comparison after changing tools or graph construction logic, pass
`--rebuild` to `codenib index` instead of reusing an older manifest.
