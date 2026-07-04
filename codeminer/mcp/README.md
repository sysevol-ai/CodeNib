<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeMiner MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes
CodeMiner's search over a **pre-built index** to LLM agents. It serves four search
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
(`codeminer.compiler`). It runs the registered index builders and writes
`<repo>/.codeminer_cache/repo_manifest.json`:

```python
from codeminer.compiler import IndexCompiler, IndexCompilerConfig
from codeminer.compiler.index_builders import (
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
# -> writes /path/to/repo/.codeminer_cache/repo_manifest.json
```

`search_semantic` needs the `vector` index, `search_bm25` needs `bm25`,
`search_regex` needs `symbol_graph` (the regex index is derived from it), and
`search_zoekt` needs `zoekt`. Builds that fail (e.g. Zoekt binary missing) are marked
`failed` in the manifest and the corresponding tool returns a clear error rather than
aborting the others.

### 2. Start the MCP server

```bash
codeminer-mcp /path/to/repo/.codeminer_cache/repo_manifest.json
# or, equivalently:
codeminer-mcp --manifest /path/to/repo/.codeminer_cache/repo_manifest.json
python -m codeminer.mcp /path/to/repo/.codeminer_cache/repo_manifest.json
```

Optional `--log-level {DEBUG,INFO,WARNING,ERROR}` (default `INFO`); logs go to stderr.

## Tools

| Tool | Backing index | Result granularity | Use for |
|------|---------------|--------------------|---------|
| `search_semantic` | `vector` | symbol (l0/l1/l2) | natural-language / conceptual queries |
| `search_bm25` | `bm25` | symbol | exact-name / keyword lookups |
| `search_regex` | `symbol_graph` | symbol | structural pattern matching |
| `search_zoekt` | `zoekt` | file | fast substring/regex across raw repo contents |
| `lsp_route` | `symbol_graph` | symbol route | graph-backed LSP-style route anchors across endpoint, bridge/factory, provider, and type nodes |
| `get_manifest` | — | — | repo metadata: path, commit, languages, capabilities |

### `search_semantic`
Vector-embedding similarity search.

- `query` (str): natural-language or code query.
- `top_k` (int, default 10): max results.
- `level` (str, default `"l2"`): hierarchy level — `"l0"` (files), `"l1"` (top-level
  symbols), `"l2"` (functions/methods).
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

### `lsp_route`
Graph-backed LSP-style route map over the static symbol graph.

- `symbols` (list[str]): symbol seeds, such as names found by `search_bm25`,
  `search_regex`, or source reads.
- `query` (str, default `""`): the natural-language task; route scoring uses it
  to classify endpoint, bridge/factory, provider, and type anchors.
- `top_k` (int, default 12): max compact anchors to return.
- `include_neighbors` (bool, default `True`): include query-relevant one-hop graph
  neighbors that fill route gaps.

Returns compact location dicts only. Callers should read source before finalizing.

### `get_manifest`
Returns the repo manifest as a dict: path, commit, languages, available indexes, and
derived capabilities.

## Prompt & status

- **Prompt `codeminer-guide`** — returns guidance on choosing between the search tools.
- **`server_status()`** — a non-tool helper (used by tests/debugging) that reports the
  repo, commit, languages, and which indexes (vector / bm25 / symbol_graph / zoekt) are
  loaded.

## Architecture

- **`ServerContext`** (`context.py`): loads the manifest and hydrates available indexes.
- **Phase 1 (indexing)**: `IndexCompiler` builds indexes and writes the manifest.
- **Phase 2 (query)**: this server loads the manifest and serves the search tools.
- **Tool implementations** live in `tools/search.py`; the `@mcp.tool` wrappers and CLI
  live in `server.py`.

## Development

```bash
# Unit tests (no MCP dependency required)
pytest test/mcp/test_mcp_server.py -v

# Integration tests (require built indexes)
pytest test/mcp -v -m integration
```
