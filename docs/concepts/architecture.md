<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Repository context architecture

CodeNib builds several views of one repository snapshot and serves the views
needed by each query. Source identity and source locations remain attached
to the artifacts and returned evidence.

| Layer | Responsibility |
| --- | --- |
| View compiler | Chunk source and build BM25, dense, graph, and navigation views; reuse current artifacts and atomically rebuild affected views |
| View manifest | Record repository identity, source fingerprint, builder profile, capabilities, status, and artifact location independently for each view |
| Context serving | Execute lexical, semantic, hybrid, reranked, and structural query plans while preserving repository-relative source locations |
| Agent runtime | Expose MCP and LSP-shaped tools, assemble bounded evidence, and return inspectable citations |

```text
repository change
  -> reuse current views or rebuild affected views
  -> publish a manifest describing available capabilities
  -> plan repository queries
  -> deliver bounded context with source locations
```

On a later commit, CodeNib reuses views whose source and builder identities
are still current. A requested view affected by source or policy changes
rebuilds in an isolated generation. File- and symbol-level delta repair
remains disabled until it can use the same pinned source authority.

## Querying from Python

```python
from codenib.agent import RepositoryContextExplorer

with RepositoryContextExplorer.from_repository(
    "/path/to/repository", policy="auto"
) as explorer:
    result = explorer.explore("where is request retry behavior implemented?", top_k=10)
```

Results carry source-validated evidence and the selected plan, capabilities,
loaded views, fusion, graph, and reranking trace. See
[RAG ops and planner](../rag_ops.md) for query planning,
[CodeGraph](../codegraph.md) for setup and source policies, and
[MCP](../mcp.md) for the tool contract.

## Wiki and portable artifacts

Wiki persistence uses the Wiki-owned `WikiStore` facade and SQLite
implementation. Repository manifests, search indexes, graphs and portable
context payloads remain file artifacts tied to the indexed source.

The [GitHub Pages workflow](../github_pages.md) publishes a static Wiki and
a matching portable context artifact. Static pages include source citations
and available page-level dependency data; interactive Ask and runtime graph
exploration belong to the local or MCP server.
