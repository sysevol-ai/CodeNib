<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Incremental Graph Patching

Update an existing `CodeGraph` in-place when code changes, avoiding full re-indexing. Uses LSP language servers to detect symbol changes and reconnect reference edges.

There are two entry points:

1. **`IndexCompiler.update_repo()`** (recommended) — the compiler drives the patcher for you, alongside every other index, under admission control. See [Compiler-Driven Updates](#compiler-driven-updates-recommended).
2. **`LSIndexer.graph_patch()`** — the low-level primitive that patches a single graph directly. The rest of this page documents this layer.

## Compiler-Driven Updates (Recommended)

`IndexCompiler.update_repo()` productionizes graph patching: it advances an existing manifest (written by an initial `compile_repo()` build, see [MCP server](../mcp.md)) to the repo's current `HEAD`, routing each requested index through its builder's `incremental_update()` instead of a full `build()`:

```python
from codenib.compiler import IndexCompiler
from codenib.compiler.index_builders import (
    IndexBuilderRegistry,
    register_default_builders,
)

registry = IndexBuilderRegistry()
register_default_builders(registry, languages=["python"])

compiler = IndexCompiler(registry)
compiler.compile_repo("/path/to/repo")   # initial full build

# ... new commits land ...
compiler.update_repo("/path/to/repo")    # incremental advance to HEAD
```

`update_repo()` compares the manifest's `last_indexed_commit` against `HEAD` and then:

- **Nothing to do** — `HEAD` unchanged and every requested index `fresh`: returns the existing manifest untouched.
- **Retry incomplete builds** — `HEAD` unchanged but some requested index is missing or not `fresh` (a previous run failed partway): re-runs a full `compile_repo()` so the failed indexes are retried instead of staying stale.
- **Full rebuild fallback** — no manifest, an unreadable manifest, or an empty `last_indexed_commit` (no complete baseline was ever established): falls back to `compile_repo()`.
- **Incremental advance** — otherwise, each builder's `incremental_update(..., last_commit=<previous>)` runs. Builders without a real delta path (BM25, Zoekt) rebuild internally, the vector builder applies a git-diff-driven embedding update, and the symbol-graph builder runs the LSP patcher described below — the result is always correct, only the cost differs.

### last_indexed_commit Semantics

The manifest claims `HEAD` as `last_indexed_commit` only when **every** requested index built successfully. If any build fails, `last_indexed_commit` is left at the previous commit (empty when no full build ever succeeded), so the next `update_repo()` retries the failed indexes rather than reporting "nothing to update" and leaving them stale forever. Failed entries are still recorded in the manifest with `status="failed"` and the error in their metadata.

### Admission Control for the Symbol Graph

An incremental symbol-graph result is only *admitted* when the patched artifact can be taken as equivalent to a fresh rebuild. `SymbolGraphBuilder` delegates that decision to the `UpdateVerifier` contract (`codenib/compiler/verification.py`):

| Verifier | Behavior |
|----------|----------|
| `NullVerifier` (default) | Proves nothing and says so: `verified=False`, `checked=False`, reason `"no verifier configured"`. |
| `AlwaysAdmitVerifier` | Admits without checking (`verified=True`, `checked=False`). An explicit opt-in for environments that have accepted the risk deliberately. |

With the builder's default `require_verification=True`, an unadmitted patch is discarded and the graph is fully rebuilt — so out of the box the symbol-graph path behaves exactly like a full rebuild until a real verifier is configured. To accept unverified patches, construct `SymbolGraphBuilder` with `AlwaysAdmitVerifier()` (or `require_verification=False`) and register it in place of the default. Either way the outcome (`verified`, `verification_checked`, `verification_reason`) is written into the manifest entry's metadata, so admission is auditable on disk. A patch that touches no source files of the configured languages is admitted directly.

Independently of verification, the builder falls back to a full rebuild whenever the incremental path cannot run safely: no previously indexed commit, no existing `graph.pkl`, an unresolvable `HEAD`, uncommitted changes to tracked files, a missing language server, or any patch failure.

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
