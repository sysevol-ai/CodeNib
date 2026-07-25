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

Each build writes
`<repo>/.codenib_cache/repo_manifest.json`. A failed optional view is recorded
in the manifest without invalidating successful independent views.

## Run The Server

```bash
codenib mcp /path/to/repository
```

The command also accepts the manifest path directly:

```bash
codenib mcp /path/to/repository/.codenib_cache/repo_manifest.json
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
| `search_regex` | `symbol_graph` | symbol | Structural pattern matching |
| `search_zoekt` | `zoekt` | file | Fast substring or regex search over files |
| `dependency_subgraph` | `symbol_graph` | call graph | Caller impact, callee dependencies, or a one-hop neighborhood |
| `lsp_definition` | `symbol_graph` | location | Static go-to-definition-shaped lookup |
| `lsp_references` | `symbol_graph` | locations | Static find-references-shaped lookup |
| `lsp_route` | `symbol_graph` | locations | Compact route anchors among related symbols |
| `get_manifest` | manifest | repository | Repository identity, languages, view states, and capabilities |

The `codenib-guide` prompt explains how to choose among available tools.
Parameter and return schemas live in
[`codenib/mcp/README.md`](https://github.com/sysevol-ai/CodeNib/blob/main/codenib/mcp/README.md).

## Advanced Views

The `full` preset requests BM25, vectors, a symbol graph, and Zoekt:

```bash
pip install "codenib[full]"
codenib index /path/to/repository --preset full
```

Graph and Zoekt construction also require external backend binaries. Check
their availability before building:

```bash
codenib doctor --require graph
```

See [SCIP Indexing](scip_index.md) and
[Language Capabilities](language_capabilities.md) for backend-specific setup
and support boundaries.
