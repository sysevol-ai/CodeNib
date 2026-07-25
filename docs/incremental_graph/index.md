<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Incremental Graph Patching

Update an existing `CodeGraph` in-place when code changes, avoiding full re-indexing. Uses LSP language servers to detect symbol changes and reconnect reference edges.

## How It Works

Given a base commit (matching the current graph) and a target commit:

1. **Detect changes** — `git diff -U0` identifies modified/added/deleted/renamed files and changed line ranges
2. **Round 1 (Vertices)** — delete old symbols, create new ones, shift line numbers for unaffected symbols
3. **Round 2 (Edges)** — reconnect reference edges using LSP `references` (incoming) and `semantic_tokens` + `definition` (outgoing)

The two-round design ensures all vertices exist before any edges are created.

### Symbol Classification

For modified files, each symbol is classified by comparing old graph data against new LSP results and git hunks: `deleted`, `added`, `affected` (overlaps changed lines), `shifted` (line offset but content unchanged), or `unchanged`. Only `affected` and `added` symbols trigger edge reconnection in Round 2.

### LSP Interaction

Each language uses a specific language server, started via stdio by `LSPClient`:

| Language | Server | Install |
|----------|--------|---------|
| Python | `basedpyright-langserver` | `make python-lsp-tool` |
| Rust | `rust-analyzer` | `make rust-tool` |
| TypeScript/JS | `typescript-language-server` | `make typescript-lsp-tool` |
| Go | `gopls` | `make gopls-tool` |
| C/C++ | `clangd` | `make active-system-deps-ubuntu clangd-tool` |

The patcher queries three LSP methods to rebuild the graph:

1. **`textDocument/documentSymbol`** — get the hierarchical symbol tree of a file (used in Round 1 to discover new/changed symbols)
2. **`textDocument/references`** — find all call-sites of a symbol across the project (used in Round 2 to reconnect incoming edges)
3. **`textDocument/semanticTokens`** + **`textDocument/definition`** — scan tokens in changed line ranges, then resolve each cross-file token to its definition (used in Round 2 to reconnect outgoing edges)

`LSPClient` auto-resolves server binaries from PATH / conda / venv / Go,
Cargo, npm-global, .NET global tools, and local user bin directories.

**C/C++ special case**: clangd's background indexer is natively incremental — it only re-indexes changed translation units, producing updated `.idx` files. So `patcher_cpp.py` simply triggers a clangd background-index run on the changed files and rebuilds the graph from the new `.idx` data, without the LSP query flow above.

## Prerequisites

- An existing `CodeGraph` (built via `LSIndexer.run_pipeline()`, see [scip_index](../scip_index.md))
- The corresponding language server installed (see table above)
- The project must be a git repository with both the base and target commits reachable

## Usage

```python
from codenib.ls_router import LSIndexer
from codenib.graph.code_graph import CodeGraph

# Load a graph built at commit v1.0
graph = CodeGraph.load_graph("/cache/project/graph.pkl")

# Patch it to HEAD
indexer = LSIndexer("/path/to/project", language="rust")
result = indexer.graph_patch(graph, base_commit="v1.0", target_commit="HEAD")

graph.save_graph("/cache/project/graph.pkl")
```

**Supported languages:** Python, Rust, TypeScript/JS, Go, C/C++.

### Return Value

`graph_patch` returns a stats dict:

```python
{
    "files_modified": 1,
    "vertices_created": 2,
    "vertices_deleted": 0,
    "vertices_shifted": 3,
    "refs_incoming": 5,
    "refs_outgoing": 3,
    "nodes_before": 45,
    "nodes_after": 47,
}
```

## Interactive Demo

See the [Interactive Demo](interactive.md) for a step-by-step visual walkthrough of the symbol classification and graph rebuild process.
