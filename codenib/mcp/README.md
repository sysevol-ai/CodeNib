<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes
CodeNib's search over a **pre-built index** to LLM agents. It serves four search
tools — vector (semantic), BM25, regex, and Zoekt trigram — plus a manifest tool, a
usage-guidance prompt, and a status helper. Transport is stdio.

## Installation

```bash
make dev
```

Zoekt search additionally requires the Go-based Zoekt binaries on `PATH`:

```bash
make zoekt-tool
eval "$(make --no-print-directory active-scip-env | sed -n 's/^  export /export /p')"
```

## Usage

The server is **query-only**: it loads a `repo_manifest.json` produced by the index
compiler and serves search over those indexes. Build the manifest first, then start
the server against it.

### 1. Build the index (writes the manifest)

There is no standalone CLI for this step — drive `IndexCompiler` from Python
(`codenib.compiler`). It runs the registered index builders and writes
`<repo>/.codenib_cache/repo_manifest.json`:

```python
from codenib.compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.index_builders import (
    IndexBuilderRegistry,
    register_default_builders,
)

registry = IndexBuilderRegistry()
register_default_builders(registry, languages=["python"])

compiler = IndexCompiler(
    registry,
    IndexCompilerConfig(
        # bm25/vector/symbol_graph are the defaults; add "zoekt" to enable
        # trigram search (requires the zoekt-git-index binary on PATH).
        index_types=["bm25", "vector", "symbol_graph", "zoekt"],
        languages=["python"],
    ),
)
manifest = compiler.compile_repo("/path/to/repo")
# -> writes /path/to/repo/.codenib_cache/repo_manifest.json
```

`search_semantic` needs the `vector` index, `search_bm25` needs `bm25`,
`search_regex` needs `symbol_graph` (the regex index is derived from it), and
`search_zoekt` needs `zoekt`. Builds that fail (e.g. Zoekt binary missing) are marked
`failed` in the manifest and the corresponding tool returns a clear error rather than
aborting the others.

### 2. Start the MCP server

```bash
codenib-mcp /path/to/repo/.codenib_cache/repo_manifest.json
# or, equivalently:
codenib-mcp --manifest /path/to/repo/.codenib_cache/repo_manifest.json
python -m codenib.mcp /path/to/repo/.codenib_cache/repo_manifest.json
```

Optional `--log-level {DEBUG,INFO,WARNING,ERROR}` (default `INFO`); logs go to stderr.

## Tools

| Tool | Backing index | Result granularity | Use for |
|------|---------------|--------------------|---------|
| `search_semantic` | `vector` | file (l0) / symbol (l2) | natural-language / conceptual queries |
| `search_bm25` | `bm25` | symbol | exact-name / keyword lookups |
| `search_regex` | `symbol_graph` | symbol | structural pattern matching |
| `search_zoekt` | `zoekt` | file | fast substring/regex across raw repo contents |
| `dependency_subgraph` | `symbol_graph` | call graph | structural dependency / impact analysis |
| `lsp_definition` | `symbol_graph` | location | static graph analogue of go-to-definition |
| `lsp_references` | `symbol_graph` | locations | static graph analogue of find-references |
| `lsp_route` | `symbol_graph` | locations | compact route anchors for related symbols |
| `get_manifest` | — | — | repo metadata: path, commit, languages, capabilities |

### `search_semantic`
Vector-embedding similarity search.

- `query` (str): natural-language or code query.
- `top_k` (int, default 10): max results.
- `level` (str, default `"l2"`): hierarchy level — `"l0"` (files), `"l2"`
  (functions/methods).
- `score_threshold` (float, default 0.0): minimum similarity; `0` disables the filter.

Returns a list of node dicts (`node_id`, `file_path`, `node_type`, `content`, `score`,
`start_line`/`end_line` 1-based). If no vector index is loaded it returns
`{"error": ...}` so callers can recover gracefully.

### `search_bm25`
BM25 keyword retrieval over indexed symbols.

- `query` (str), `top_k` (int, default 20), `filter_test` (bool, default `False`) —
  when `True`, excludes results from test files.

### `search_regex`
Grep-like regex over CodeGraph nodes.

- `pattern` (str): Python `re` pattern.
- `top_k` (int, default 20).
- `file_glob` (str, default `""`): restrict by file path (e.g. `*.py`).
- `node_type` (str, default `""`): filter by type (`function`, `class`, `method`,
  `file`, …).
- `case_sensitive` (bool, default `False`).

### `search_zoekt`
Trigram search over **raw repository contents** (not the CodeGraph), so results are
file-level (`type="file"`).

- `query` (str): plain substring, regex (`r:foo`), or atoms like `case:yes` /
  `lang:python`.
- `top_k` (int, default 20).
- `file_filter` (str, default `""`): glob/regex appended as `file:<expr>`.

### `lsp_definition` / `lsp_references` / `lsp_route`
Static LSP-shaped navigation over the loaded `symbol_graph`. These tools return
compact locations only; clients should read source before finalizing.

- `lsp_definition`: provide either `symbol` or `file_path` + 1-based `line`.
- `lsp_references`: same inputs, with optional `include_declaration`.
- `lsp_route`: provide one or more `symbols` plus optional `query` to rank
  endpoint, bridge/factory, provider/value, and type anchors.

### `get_manifest`
Returns the repo manifest as a dict: path, commit, languages, available indexes, and
derived capabilities.

## Prompt & status

- **Prompt `codenib-guide`** — returns guidance on choosing between the search tools.
- **`server_status()`** — a non-tool helper (used by tests/debugging) that reports the
  repo, commit, languages, and which indexes (vector / bm25 / symbol_graph / zoekt) are
  loaded.

## Architecture

- **`ServerContext`** (`context.py`): loads the manifest and hydrates available indexes.
- **Phase 1 (indexing)**: `IndexCompiler` builds indexes and writes the manifest.
- **Phase 2 (query)**: this server loads the manifest and serves the search tools.
- **Tool implementations** live in `tools/*.py`; the `@mcp.tool` wrappers and CLI
  live in `server.py`.

## Development

```bash
# Unit tests (no MCP dependency required)
pytest test/mcp/test_mcp_server.py -v

# Integration tests (require built indexes)
pytest test/mcp -v -m integration
```
