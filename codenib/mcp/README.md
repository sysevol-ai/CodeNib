<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes
CodeNib's search and static-navigation views over a **pre-built repository
manifest** to coding agents. Transport is stdio; protocol logs go to stderr.

## Installation

```bash
pip install "codenib[mcp]"
```

From a source checkout, `make dev` installs development and test dependencies.
Zoekt search additionally requires its Go binaries on `PATH`:

```bash
make zoekt-tool
eval "$(make --no-print-directory active-scip-env | sed -n 's/^  export /export /p')"
```

## Usage

The MCP server is **query-only**. Build or update the repository views first,
then serve their manifest.

### 1. Build the repository

The default `auto` preset builds BM25+dense when semantic dependencies are
installed and otherwise selects the no-model BM25 path:

```bash
codenib index /path/to/repo
```

Select a richer preset when those tools are needed:

```bash
pip install "codenib[full]"
codenib index /path/to/repo --preset full
```

CodeNib writes the manifest below
`$CODENIB_HOME/repositories/<repo>-<id>/indexes` (default
`~/.codenib/repositories/...`) and prints its exact path. `search_context`
plans over the loaded `bm25`, `vector`, and `symbol_graph` views;
`search_semantic` forces `vector`, while `search_regex` and the LSP-shaped tools
need `symbol_graph`, and `search_zoekt` needs `zoekt`. A failed optional view is
recorded without discarding successful independent views.

### 2. Start the MCP server

```bash
codenib mcp /path/to/repo
```

`codenib mcp` accepts either the repository directory or the exact
`repo_manifest.json` path printed by `codenib index`. For compatibility, the
`codenib-mcp <manifest>` and `python -m codenib.mcp <manifest>` entry points
also remain available. All forms accept
`--log-level {DEBUG,INFO,WARNING,ERROR}` (default `INFO`).

## Tools

| Tool | Backing index | Result granularity | Use for |
|------|---------------|--------------------|---------|
| `search_context` | loaded `bm25`, `vector`, `symbol_graph` | file / symbol | recommended capability-aware ranked retrieval |
| `search_semantic` | `vector` | file (l0) / symbol (l2) | natural-language / conceptual queries |
| `search_bm25` | `bm25` | symbol | exact-name / keyword lookups |
| `search_regex` | `symbol_graph` | file / symbol | structural pattern matching |
| `search_zoekt` | `zoekt` | file | fast substring/regex across raw repo contents |
| `dependency_subgraph` | `symbol_graph` | call graph | structural dependency / impact analysis |
| `lsp_definition` | `symbol_graph` | location | static graph analogue of go-to-definition |
| `lsp_references` | `symbol_graph` | locations | static graph analogue of find-references |
| `lsp_route` | `symbol_graph` | locations | compact route anchors for related symbols |
| `get_manifest` | — | — | repo metadata: path, commit, languages, capabilities |

All source locations returned by MCP use 1-based line numbers. Internal indexes
remain 0-based; the MCP adapters perform the conversion once at the boundary.

### `search_context`
Plans and executes ranked retrieval without asking the agent to choose an index.

- `query` (str): repository question or code query.
- `top_k` (int, default 10): results returned, from 1 through 100.
- `budget` (str, default `"balanced"`): `"fast"`, `"balanced"`, or
  `"thorough"`.
- `level` (str, default `"l2"`): dense file (`"l0"`) or symbol (`"l2"`)
  granularity.
- `filter_test` (bool, default `False`): excludes test files from BM25 branches.

The response contains the concrete `plan`, repository/commit/source-fingerprint
provenance under `source`, and 1-based source-linked `results`. Reranking is not
hidden in this tool; the current MCP route uses deterministic retrieval fusion
and graph expansion only.

### `search_semantic`
Vector-embedding similarity search.

- `query` (str): natural-language or code query.
- `top_k` (int, default 10): max results.
- `level` (str, default `"l2"`): hierarchy level — `"l0"` (files), `"l2"`
  (functions/methods).
- `score_threshold` (float, default 0.0): minimum similarity; `0` disables the filter.

Returns a list of node dicts (`node_name`, `node_id`, `file`, `type`, `content`,
`score`, `start_line`/`end_line` 1-based). If no vector index is loaded it
returns `{"error": ...}` so callers can recover gracefully.

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

- `query` (str): plain substring, regex (`regex:foo`), or atoms like `case:yes` /
  `lang:python`.
- `top_k` (int, default 20).
- `file_filter` (str, default `""`): glob/regex appended as `file:<expr>`.

### `dependency_subgraph`
Call-graph neighborhood or transitive impact analysis.

- `symbol` (str): readable symbol seed; fuzzy resolution is supported.
- `direction` (str, default `"both"`): `"impact"` for transitive callers,
  `"dependencies"` for transitive callees, or `"both"` for a neighborhood.
- `depth` (int, default 2): traversal depth, clamped to at least 1.
- `max_nodes` (int, default 60): node budget for transitive traversals.

Returns `root`, `direction`, `nodes`, `edges`, `truncated`, and `note`.
Each node's optional `line` is 1-based.

### `lsp_definition` / `lsp_references` / `lsp_route`
Static LSP-shaped navigation over the loaded `symbol_graph`. These tools return
compact locations only; clients should read source before finalizing.
All three tools require integer `top_k` values from 1 through 100.
Paths are limited to 4,096 characters, individual symbol names to 1,024
characters, and route queries to 16,000 characters so malformed requests fail
before graph resolution.

- `lsp_definition`: provide either `symbol`, or `file_path` + 1-based `line`
  with optional 0-based `character`; `top_k` defaults to 8.
- `lsp_references`: accepts the same position/symbol inputs;
  `include_declaration` defaults to `true` and `top_k` to 40.
- `lsp_route`: provide `symbols`, with optional `query`, `top_k` (default 12),
  and `include_neighbors` (default `true`) to rank endpoint, bridge/factory,
  provider/value, and type anchors. If no reliable symbol is known, pass
  `symbols=[]` with a non-blank query to select route seeds from the graph.
  At most 100 non-empty symbol seeds are accepted per request; comma-separated
  seed text is limited to 16,000 characters before splitting.

### `get_manifest`
Returns the manifest version; a nested `repo` object containing path, commit,
languages, source fingerprints, and file count; the `indexes` mapping;
`capabilities`; and compilation timestamps.

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
