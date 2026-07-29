<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# MCP Server

CodeNib serves a pre-built repository manifest to coding agents over
[Model Context Protocol](https://modelcontextprotocol.io/) stdio. Index
construction and query serving are separate: build or update a repository once,
then reuse that manifest across agent sessions.

## Install And Index

```bash
pip install "codenib[mcp]"
codenib index /path/to/repository
```

The default `fast` preset builds BM25 without a model download. Add semantic
search when needed:

```bash
pip install "codenib[mcp,semantic]"
codenib index /path/to/repository --preset semantic
```

Add static navigation and dependency tools without the embedding download:

```bash
pip install "codenib[graph,mcp]"
codenib index /path/to/repository --preset graph
```

Each build writes a manifest below
`$CODENIB_HOME/repositories/<repo>-<id>/indexes` (default
`~/.codenib/repositories/...`) and prints its exact path. A failed optional
view is recorded in the manifest without invalidating successful independent
views.

## Run The Server

```bash
codenib mcp /path/to/repository
```

The command also accepts the manifest path directly:

```bash
codenib mcp ~/.codenib/repositories/<repo>-<id>/indexes/repo_manifest.json
```

Transport is stdio and logs go to stderr. A typical client configuration is:

```json
{
  "mcpServers": {
    "codenib": {
      "command": "codenib",
      "args": ["mcp", "/absolute/path/to/repository"]
    }
  }
}
```

Use an absolute repository path because the client may launch the server from a
different working directory.

## Tools

Only tools whose backing views are fresh and available can return results.

| Tool | Backing view | Granularity | Use for |
|---|---|---|---|
| `search_semantic` | `vector` | file/symbol (L0/L2) | Natural-language or conceptual queries |
| `search_bm25` | `bm25` | symbol | Exact names and keyword lookups |
| `search_regex` | `symbol_graph` | file / symbol | Structural pattern matching |
| `search_zoekt` | `zoekt` | file | Fast substring or regex search over files |
| `dependency_subgraph` | `symbol_graph` | call graph | Caller impact, callee dependencies, or a one-hop neighborhood |
| `lsp_definition` | `symbol_graph` | location | Static go-to-definition-shaped lookup |
| `lsp_references` | `symbol_graph` | locations | Static find-references-shaped lookup |
| `lsp_route` | `symbol_graph` | locations | Compact route anchors among related symbols |
| `get_manifest` | manifest | repository | Repository identity, languages, view states, and capabilities |

All source locations returned by MCP use 1-based line numbers.

The `codenib-guide` prompt explains how to choose among available tools.
Parameter and return schemas live in
[`codenib/mcp/README.md`](https://github.com/sysevol-ai/CodeNib/blob/main/codenib/mcp/README.md).

## Advanced Views

The `full` preset requests BM25, vectors, a symbol graph, and Zoekt:

```bash
pip install "codenib[full]"
codenib index /path/to/repository --preset full
```

Graph and Zoekt construction also require external backend binaries. Check the
repository's language-specific graph provider before building:

```bash
codenib doctor /path/to/repository --require graph
```

The doctor command does not currently diagnose Zoekt. Verify both Zoekt
commands independently:

```bash
command -v zoekt-git-index
command -v zoekt-webserver
```

See [SCIP Indexing](scip_index.md) and
[Language Capabilities](language_capabilities.md) for backend-specific setup
and support boundaries.
