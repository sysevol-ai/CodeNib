<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Incremental Graph Patching

CodeNib contains a low-level, experimental graph patcher for studying safe
file- and symbol-level updates. Most languages use an LSP server; C/C++ can
refresh clangd `.idx` data in a direct experimental workflow.

> **0.2.2 production gate:** `IndexCompiler.update_repo()` reuses views whose
> source and builder identities are still current, but atomically rebuilds
> requested views affected by a source or policy change. By default it does
> not invoke builder delta paths. The one exception is opt-in: a Python graph
> can be patched by its builder, which reads what changed from the pinned
> source and not from Git. See
> [Rebuilding changed files with the builder](#rebuilding-changed-files-with-the-builder-python).

There are two distinct surfaces:

1. **`IndexCompiler.update_repo()`** (recommended) — reuse current views and
   rebuild changed requested views under manifest admission control.
2. **`LSIndexer.graph_patch()` and builder `incremental_update()` methods** —
   low-level experimental APIs. The rest of this page documents that layer;
   the production compiler does not call the language-server patcher.

## Compiler-Driven Updates (Recommended)

`IndexCompiler.update_repo()` advances an existing manifest (written by an
initial `compile_repo()` build, see [MCP server](../mcp.md)) to the repository's
current authenticated source identity:

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
compiler.update_repo("/path/to/repo")    # reuse current or atomically rebuild
```

`update_repo()` compares the manifest's `last_indexed_commit` against `HEAD` and then:

- **Nothing to do** — `HEAD` unchanged and every requested index `fresh`: returns the existing manifest untouched.
- **Retry incomplete builds** — `HEAD` unchanged but some requested index is missing or not `fresh` (a previous run failed partway): re-runs a full `compile_repo()` so the failed indexes are retried instead of staying stale.
- **Full rebuild fallback** — no manifest, an unreadable manifest, or an empty `last_indexed_commit` (no complete baseline was ever established): falls back to `compile_repo()`.
- **Changed source or policy** — each affected requested view runs its full
  builder and publishes one complete generation. BM25, vector, symbol graph,
  and Zoekt do not mutate the live generation in place.

### last_indexed_commit Semantics

The manifest claims `HEAD` as `last_indexed_commit` only when **every** requested index built successfully. If any build fails, `last_indexed_commit` is left at the previous commit (empty when no full build ever succeeded), so the next `update_repo()` retries the failed indexes rather than reporting "nothing to update" and leaving them stale forever. Failed entries are still recorded in the manifest with `status="failed"` and the error in their metadata.

### Admission Control for the Symbol Graph

The following contract applies only when callers invoke the experimental
`SymbolGraphBuilder.incremental_update()` API directly. An incremental
symbol-graph result is only *admitted* when the patched artifact can be taken
as equivalent to a fresh rebuild. `SymbolGraphBuilder` delegates that decision
to the `UpdateVerifier` contract (`codenib/compiler/verification.py`):

| Verifier | Behavior |
|----------|----------|
| `NullVerifier` (default) | Proves nothing and says so: `verified=False`, `checked=False`, reason `"no verifier configured"`. |
| `AlwaysAdmitVerifier` | Admits without checking (`verified=True`, `checked=False`). An explicit opt-in for environments that have accepted the risk deliberately. |
| `FactProjectionVerifier` | Compares the facts a patched graph states with those of a fresh build: vertices and anchored edges as multisets, identified by unified name. It needs the fresh graph, so it measures a patch rather than saving a build, and it does not replay requests against the persisted graph. |

With the builder's default `require_verification=True`, an unadmitted patch is discarded and the graph is fully rebuilt — so out of the box the symbol-graph path behaves exactly like a full rebuild until a real verifier is configured. To accept unverified patches, construct `SymbolGraphBuilder` with `AlwaysAdmitVerifier()` (or `require_verification=False`) and register it in place of the default. Either way the outcome (`verified`, `verification_checked`, `verification_reason`) is written into the manifest entry's metadata, so admission is auditable on disk. A patch that touches no source files of the configured languages is admitted directly.

Independently of verification, the builder falls back to a full rebuild
whenever the incremental path cannot run safely: no previously indexed commit,
no existing `graph.pkl`, an unresolvable `HEAD`, uncommitted changes to tracked
files, an occurrence sidecar without a matching delta contract, a missing
language server, or any patch failure.

For TypeScript and TSX, schema 5 also protects the separate file-to-file import
layer. Before starting the language server or mutating the graph, the patcher
compares tree-sitter fingerprints of static imports and source-bearing
re-exports at the indexed and target commits. A changed statement or anchor,
an added or deleted file, or a rename requests a full rebuild; a body-only edit
may remain incremental in the direct experimental API. That API catches an
explicit rebuild request and falls back to a full generation, so it cannot
silently retain stale import edges.

### What a patch keeps and what it looks up again

A patch is exact only where it does not ask the language server. The graph was
built by one analyser and the language server is another, so the patcher keeps
every fact the build produced that an edit did not touch, and looks up only
the edited lines:

- **File scope.** A changed file with no vertex in the graph was outside the
  build's scope, for example a `tests/` directory when the project limits its
  analyser to `src/`. It is not patched in.
- **Lines.** Definitions and reference anchors on untouched lines move by the
  lines added above them, computed from the file's exact edits. This covers
  what `documentSymbol` does not list: overloads that share a name, module
  variables, and references made by a module's top-level code.
- **Changed bodies.** In a function whose body changed, references on
  untouched lines are kept; only the edited lines are looked up again.
- **Refusal.** When two definitions share a name and the edit touches one that
  is not tracked, the patcher asks for a full rebuild instead of guessing.

What remains inexact is on the edited lines themselves: the two analysers can
disagree on what an edited line defines or refers to.

### Rebuilding changed files with the builder (Python)

For Python there is a second way to patch that removes that disagreement: have
the graph's own builder check the changed files, and replace their part of the
graph. It does not start a language server.

```python
from codenib.graph.incremental.builder_patch import rebuild_changed_files
from codenib.graph.incremental.change_mgr import detect_changed_files

changed = detect_changed_files(repo, base_commit, "HEAD", extensions={".py"})
rebuild_changed_files(graph, repo, changed, symbol_table=index_dir / "pyrefly-symbols.json.gz")
```

- **Why it is exact.** pyrefly reports a file the same whether it is checked
  alone or with the whole project. References from the changed files resolve
  through `pyrefly-symbols.json.gz`, the symbol table a full pyrefly build
  writes beside the graph; without it the rest of the project would look like
  an external library.
- **What else it rebuilds.** When the changed files gain or lose a definition,
  the files that refer into them are rebuilt too, because their references can
  now resolve differently. Past 200 such files it reports
  `dependents_skipped` and leaves them.
- **What it cannot see.** A file with no reference into the changed files
  whose references still resolve differently, such as an import that only now
  finds its target.
- **When it refuses.** A graph built by another provider, or whose symbol
  table is missing or was written by an older version, raises
  `BuilderPatchUnavailable`; rebuild once. So does a change that only removes
  files: with nothing to check, the builder cannot bring the rest of the graph
  up to the new commit.

The compiler uses it when you ask it to:

```bash
CODENIB_GRAPH_INCREMENTAL=builder codenib index . --preset graph --language python
```

With that setting, a stale Python graph is patched instead of rebuilt when all
of these hold; otherwise the graph is rebuilt in full, as it always is without
the setting:

- the previous graph is complete, from the same builder and source selection,
  and recorded which files it was built from;
- the graph and position index on disk are the files that build recorded;
- the graph covers Python only and was built by pyrefly;
- the change includes at least one Python file the builder can check, and
  leaves `pyproject.toml` and `pyrightconfig.json` at the project root as
  they were, since those decide which files are checked and the version every
  symbol carries;
- at most 200 files refer into the changed files when definitions were added
  or removed.

What changed is read from the previous build's recorded file contents and the
source the compiler has just fingerprinted, not from Git. The patched
generation updates the graph and the position index together and carries the
same receipts as a full build, so the MCP server and the guard load it like
any other. Its entry records `update_mode: incremental` and `patch: builder`,
and `verified: false`: it is admitted by your setting, not by a comparison.

### How exact a patch is

Measured by building the graph at a commit's parent, patching to the commit,
building fresh at the commit, and comparing every vertex and anchored edge.
"Equal" means no fact differs. `scripts/profiling/check_patch_exactness.py`
runs the comparison for one commit.

| Language | Patch | Commits equal to a fresh build | Lowest edge F1 |
|---|---|---|---|
| Python | builder | 20 of 20 (requests, xarray, sphinx, django) | 1.0 |
| Python | language server | 9 of 20 | 0.997 |
| Go | language server | 1 of 3 (gin) | 0.981 |
| JavaScript | language server | 0 of 3 (preact) | 0.78 |
| Rust | language server | 0 of 4 (byteorder) | 0.73 |

The language-server patcher is close on Python and Go and not on JavaScript
and Rust, where definitions made by macros and re-exports are not what
`documentSymbol` lists. Only Python has a builder patch. Until a patch is
equal on the commits you care about, treat a patched graph as a fast
approximation and rebuild before relying on it.

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
