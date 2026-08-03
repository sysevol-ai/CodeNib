<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Contributing a Language

Language support in CodeNib is capability-based. A language can support
tree-sitter chunking without a dependency graph, or add cold-start indexing,
incremental patching, and C++ decoding independently.

Use [`codenib/languages.py`](https://github.com/sysevol-ai/CodeNib/blob/main/codenib/languages.py)
as the source of truth. The generated [Language Capabilities](language_capabilities.md)
page shows the capabilities currently exposed by each registered language.

## Integration layers

| Layer | Registry fields | Implementation |
| --- | --- | --- |
| Chunking | `chunker_*`, `chunk_extensions` | `codenib/code_chunking/` |
| Ground truth | `gt_language`, `gt_extensions` | `codenib/dataset/gt_locate.py` |
| Agent language names | `agent_languages`, `agent_aliases` | `codenib/agent/compile.py` |
| Cold-start graph | `graph_*`, `cold_start_backend` | `codenib/ls_router.py`, `codenib/scip_interface/`, `codenib/ls_index/` |
| Incremental graph | `incremental_backend`, `incremental_patcher` | `codenib/graph/incremental/` |
| LSP route | `lsp_*` | `codenib/ls_index/lsp_indexer.py` |
| Core decoder | `core_decoder`, `core_decoder_aliases` | `core/`, `codenib/scip_interface/scip_decode_core.py` |

Keep surface-specific aliases explicit. For example, `.c` files use the C++
chunker and graph backend, while the agent-facing language key may remain `c`.

## 1. Register the language

Add one `LanguageSpec` to `LANGUAGE_SPECS`. Set only capabilities that are
implemented and tested:

```python
LanguageSpec(
    key="example",
    display_name="Example",
    aliases=("ex",),
    chunker_language="example",
    chunker_aliases=("example", "ex"),
    chunker_class="codenib.code_chunking.example_chunker:ExampleCodeChunker",
    chunk_extensions=(".ex",),
    gt_language="example",
    gt_extensions=(".ex",),
    graph_language="example",
    graph_aliases=("example", "ex"),
    graph_extensions=(".ex",),
    agent_languages=("example",),
    agent_aliases=(("example", "example"), ("ex", "example")),
    cold_start_backend="lsp",
    graph_indexer="codenib.ls_index.lsp_indexer:GenericLSPIndexer",
    graph_decoder="codenib.ls_index.lsp_graph_decode:GenericLSPGraphDecoder",
    lsp_language_id="example",
    lsp_command=("example-language-server", "--stdio"),
    lsp_command_env="CODENIB_EXAMPLE_LSP_CMD",
    core_decoder=False,
)
```

Add registry tests in `test/test_languages.py` for aliases, extensions,
backend resolution, and any core-decoder declaration.

The scaffold command can create a reviewed starting point:

```bash
python scripts/scaffold_language.py example \
  --display-name Example \
  --extension .ex \
  --alias ex \
  --graph-backend lsp \
  --incremental-backend lsp
```

The command is a dry run unless `--write` is supplied. Generated files contain
implementation stubs and are not registered automatically.

## 2. Add tree-sitter chunking

For retrieval or ground-truth extraction:

1. Add `codenib/code_chunking/{lang}_chunker.py`.
2. Export the chunker from `codenib/code_chunking/__init__.py`.
3. Register its import path in `chunker_class`.
4. Add focused tests under `test/chunker/`.

`create_chunker()` resolves the class from `LanguageSpec`; do not add a second
per-language dispatch table. `CodeChunk.start_line` and `end_line` are 0-based.

A chunking-only language should leave graph and patcher fields unset. Public
capability reporting will then advertise retrieval without claiming dependency
graph support.

## 3. Add a cold-start graph backend

Choose the backend that produces the most reliable repository-level graph:

- **SCIP:** implement an indexer and decoder under
  `codenib/scip_interface/`, then register their import paths.
- **clangd artifact:** use `codenib/ls_index/` for a backend that consumes
  clangd `.idx` files.
- **generic LSP:** register `GenericLSPIndexer`,
  `GenericLSPGraphDecoder`, the language id, and the server command. The shared
  driver uses LSP document symbols, definitions, references, and call
  hierarchy when the server exposes them.

The resulting graph must use the constants in `codenib/types.py` and preserve
the CodeGraph contract:

- vertices carry `type`, `file`, `start_line`, `end_line`, and
  `unified_name`;
- containment and reference edges use the centralized edge types;
- reference edges preserve `anchor_file` and `anchor_line` when the backend
  provides a source location;
- graph source lines and anchors remain 0-based internally.

Do not create a separate persisted schema for a new backend. If the shared
persisted graph schema changes, update the graph schema version and keep the
Python and C++ decoders in parity.

### Toolchain and smoke checks

Install the repository-pinned tools and verify command resolution:

```bash
make multilang-tools
make toolchain-doctor
```

For an active SCIP route on a real repository:

```bash
make scip-project-smoke \
  PROJECT_LANGUAGE=example \
  PROJECT_ROOT=/path/to/project
```

For a registered LSP route:

```bash
make lsp-project-smoke \
  PROJECT_LANGUAGE=example \
  PROJECT_ROOT=/path/to/project
```

When both routes are registered, compare them against the same checkout:

```bash
make graph-route-alignment \
  PROJECT_LANGUAGE=example \
  PROJECT_ROOT=/path/to/project
```

`GRAPH_ALIGNMENT_REFERENCE_ROUTE`,
`GRAPH_ALIGNMENT_CANDIDATE_ROUTE`, `GRAPH_ALIGNMENT_TARGET_DIR`,
`GRAPH_ALIGNMENT_EXCLUDE_PATTERNS`, and `GRAPH_ALIGNMENT_EXTRA_ARGS` customize
the comparison. The alignment report compares definitions by `unified_name`,
symbol containment, and reference statistics.

Toolchain commands are installed under `CODENIB_SCIP_TOOLS_DIR`. Use
`make active-scip-env`, `make lsp-smoke-env`, or
`make scip-cold-start-env` when running the underlying scripts manually.

## 4. Add incremental patching

Only add incremental support after the cold-start graph contract is stable:

1. Implement a `PatcherBase` subclass in
   `codenib/graph/incremental/patcher_{lang}.py`.
2. Register its import path in `LanguageSpec.incremental_patcher`.
3. Reuse registry-derived graph extensions.
4. Add focused tests under `test/graph/incremental/`.

`GraphPatcher` resolves patcher classes from the language registry. A language
without `incremental_patcher` remains cold-start-only.

## 5. Add dataset and agent coverage

Verify the registry-backed consumers rather than introducing local alias maps:

- repository chunking discovers the intended extensions through
  `RepoChunkingConfig`;
- `GTLocator` maps affected files through the registered GT extensions;
- agent scenarios resolve the intended language keys and aliases;
- benchmark-specific language groups are changed only when the dataset
  contains that language.

## 6. Add optional C++ decoding

Core decoding is optional. When `core_decoder=True`:

1. implement the decoder under `core/`;
2. expose it through `codenib/scip_interface/scip_decode_core.py`;
3. pass every graph field through both the Python and C++ paths;
4. add or extend the parity case in `test/scip/test_scip_core.py`.

Parity covers the vertex-name set, edge multiset, anchors, and per-vertex
`type`, `file`, line range, and `unified_name`.

## Verification

Start with the changed surface:

```bash
python -m pytest -q \
  test/test_languages.py \
  test/chunker/test_{lang}_chunker.py
```

For repository discovery:

```bash
python -m pytest -q \
  test/test_languages.py \
  test/chunker/test_repo_chunking_config.py
```

For graph, incremental, or core changes, add the matching tier:

```bash
python -m pytest -q test/scip/test_scip_indexer.py
python -m pytest -q test/scip/test_scip_multilingual.py -k {lang}
python -m pytest -q test/graph/incremental -k {lang}
python -m pytest -q test/scip/test_scip_core.py -k {lang}
```

Finally verify that the registry and generated public matrix agree:

```bash
make multilang-registry-check
```

External toolchains and fixture repositories can cause local integration tests
to skip. Accelerated languages must run their parity cases in the CI tier that
provides the required artifacts.

## Pull request checklist

- [ ] `LanguageSpec` is the first source of aliases and capabilities.
- [ ] Chunking, graph, incremental, and core support are declared separately.
- [ ] New aliases and extensions have registry tests.
- [ ] Graph nodes, edges, anchors, and line bases follow the shared contract.
- [ ] Cold-start and incremental backends have focused tests.
- [ ] Python/C++ parity covers every accelerated decoder.
- [ ] `make multilang-registry-check` passes.
