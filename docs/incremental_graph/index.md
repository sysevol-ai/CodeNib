<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Incremental Graph Patching

Update an existing `CodeGraph` in place when code changes, avoiding a full
re-index whenever the language backend supports a safe delta. Most languages
use an LSP server; C/C++ refreshes clangd `.idx` data instead.

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

For TypeScript and TSX, schema 5 also protects the separate file-to-file import
layer. Before starting the language server or mutating the graph, the patcher
compares tree-sitter fingerprints of static imports and source-bearing
re-exports at the indexed and target commits. A changed statement or anchor,
an added file, or a rename requests a full rebuild; a body-only edit may remain
incremental. The compiler catches that explicit request and rebuilds, so an
incremental update cannot silently retain stale import edges.

## How It Works

Given a base commit (matching the current graph) and a target commit:

1. **Detect files and hunks** — `git diff --name-status` identifies
   modified/added/deleted/renamed files; `git diff -U0` provides inclusive,
   0-based old/new hunk ranges.
2. **Round 1 (prepare vertices)** — all changed files are classified before
   cross-file edges are queried. Added symbols are created, deleted symbols
   are removed, and existing shifted/affected symbols are updated in place.
3. **Round 2 (connect edges)** — affected symbols rediscover outgoing
   references; added symbols discover both incoming and outgoing references.
   Edge writes are batched after all Round 1 vertices exist.
4. **Refresh range indexes** — the `O(V + E)` line-span and call-site indexes
   are rebuilt so `query_range()` immediately sees the post-patch graph.

The two-round design ensures a definition added in one changed file exists
before another changed file tries to connect a reference to it.

### Symbol Classification

For modified files, the patcher compares the existing graph with the new
`documentSymbol` snapshot and classifies each symbol:

| Class | Meaning | Patch action |
|------|---------|--------------|
| `deleted` | Present only in the old snapshot and its old span overlaps a hunk | Remove the vertex |
| `added` | Present only in the new snapshot | Create the vertex and discover incoming/outgoing references |
| `affected` | Stable identity, but its new span overlaps a hunk | Keep the vertex; refresh stale outgoing references |
| `shifted` | Stable identity and body, but its start line moved | Rename/update the vertex and shift its anchored outgoing edges |
| `unchanged` | Stable identity and line | Leave it untouched |
| `invisible` | Present in the baseline graph but absent from `documentSymbol`, outside every hunk | Preserve it; this covers definitions the LSP outline cannot represent |

An `affected` symbol takes one of two paths. If every overlapping hunk
preserves line count, only outgoing edges anchored in the changed ranges are
removed and rediscovered; surviving anchors are shifted uniformly. If a hunk
changes line count, all outgoing references for that symbol are cleared and
its full body is rescanned. Incoming edges remain attached because the vertex
is updated rather than deleted.

When a container overlaps a hunk only because one of its children is affected,
the classifier demotes the container to `shifted`. Its span metadata is still
refreshed, but the child owns the reference rescan and avoids duplicate work.

### Severed-edge contract

A full-file rebuild can temporarily cut references between the rebuilt file
and the rest of the graph. Each cut call site is recorded separately as:

```text
(
  source_name,
  target_name,
  source_unified_name,
  target_unified_name,
  anchor_file,
  anchor_line,
)
```

This is a six-element tuple, not a source/target pair: parallel references
between the same symbols remain distinct when their call-site anchors differ.
Incoming edges are safe to remap to the rebuilt target. Outgoing edges are
remapped only when the source body is still trustworthy; otherwise LSP
discovery replaces them. Recorded anchor lines are preserved or rebased
through a known file shift before the edge is restored.

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

**C/C++ special case**: clangd's background indexer refreshes `.idx` files
for affected translation units. `patcher_cpp.py` removes the old changed-file
subgraphs, reads the refreshed `.idx` data to restore their vertices and
references, then rebuilds the same range indexes as the generic patcher.
Header changes can invalidate multiple translation units, so that path may do
more work than the source-file list alone suggests. The `.idx` refresh is
prepared before graph mutation; if reindexing, decoding, merging, or range-index
construction fails, the patcher restores the original in-memory graph and
raises instead of returning a partially updated graph.

## Prerequisites

- An existing `CodeGraph` (built via `LSIndexer.run_pipeline()`, see [scip_index](../scip_index.md))
- The corresponding language server installed (see table above)
- The project must be a git repository with both the base and target commits reachable
- The checked-out, tracked working tree must represent `target_commit`.
  Symbol and `.idx` snapshots come from files on disk; the compiler path
  enforces a clean tree and uses the resolved current `HEAD` as its target.

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

`base_commit` is passed into the patcher as `earlier_commit`, and
`target_commit` as `later_commit`. The same pair drives both changed-file
detection and every modified file's old/new hunk coordinates. Direct
`patch_files()` callers may omit `later_commit`; its effective value is then
`HEAD`. The returned `commit_earlier` and `commit_later` fields record the
effective pair used by the patch.

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

See the [Interactive Demo](interactive.md) for a step-by-step walkthrough of
classification, in-place vertex preparation, reference discovery, and the
final range-index refresh.
