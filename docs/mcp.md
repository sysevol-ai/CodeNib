<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# MCP Server

CodeMiner ships a [Model Context Protocol](https://modelcontextprotocol.io/) server
(`codeminer-mcp`) that exposes its search over a **pre-built index** to LLM agents.
It is query-only: build a repository index once, then point the server at the
resulting manifest.

## Build the index

Indexes are compiled with `IndexCompiler` (`codeminer.compiler`), which writes
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
    IndexCompilerConfig(index_types=["bm25", "vector", "symbol_graph", "zoekt"]),
)
compiler.compile_repo("/path/to/repo")
```

Each tool depends on a specific index (see the table below). A build that fails — for
example Zoekt when its binary is missing — is recorded as `failed` in the manifest and
only that tool returns an error; the others still work.

## Run the server

```bash
codeminer-mcp /path/to/repo/.codeminer_cache/repo_manifest.json
```

Transport is stdio; logs go to stderr (`--log-level` to adjust).

## Tools

| Tool | Backing index | Granularity | Use for |
|------|---------------|-------------|---------|
| `search_semantic` | `vector` | symbol (l0/l1/l2) | natural-language / conceptual queries |
| `search_bm25` | `bm25` | symbol | exact-name / keyword lookups |
| `search_regex` | `symbol_graph` | symbol | structural pattern matching |
| `search_zoekt` | `zoekt` | file | fast substring/regex across raw file contents |
| `dependency_subgraph` | `symbol_graph` | call graph | structural "who calls X / what does X reach" — `impact` (transitive callers / blast radius), `dependencies` (transitive callees), or `both` (1-hop neighborhood); returns nodes+edges JSON |
| `get_manifest` | — | — | repo metadata: path, commit, languages, capabilities |

A `codeminer-guide` prompt returns guidance on choosing between these tools.

See [`codeminer/mcp/README.md`](https://github.com/sysevol-ai/CodeMiner/blob/main/codeminer/mcp/README.md)
for full parameter and return-shape details.
