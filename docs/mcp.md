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

This page describes the current `0.2` alpha. The recommended agent-serving path
installs MCP with semantic retrieval; the default `auto` preset resolves to
BM25+dense in that environment:

```bash
python -m pip install "codenib[mcp,semantic]==0.2.0"
codenib index /path/to/repository
```

Use the smaller no-model fallback when semantic retrieval is not required:

```bash
python -m pip install "codenib[mcp]==0.2.0"
codenib index /path/to/repository --preset fast
```

Add static navigation and dependency tools without the embedding download:

```bash
python -m pip install "codenib[graph,mcp]==0.2.0"
codenib toolchain install /path/to/repository --scope graph
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

### Load a Published Artifact

A Pages publishing run can produce the same query-serving views once and reuse
them in MCP at the indexed commit:

```bash
codenib artifact fetch owner/repository --repo /path/to/repository
codenib artifact mcp-config \
  ~/.codenib/artifacts/owner/repository/<full-commit> \
  --repo /path/to/repository \
  --host codex
```

`artifact fetch` derives the full commit from the checkout unless `--commit` is
provided. It requires `GH_TOKEN` with Actions read access. The MCP process
rechecks artifact hashes, repository identity, commit, and the filtered source
fingerprint on every start; it does not rebuild or silently substitute a stale
view. See [Publish With GitHub Pages](github_pages.md#reuse-the-artifact-through-mcp)
for the trust boundary and Claude/generic configuration options.

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

### MCP Registry

CodeNib publishes its local stdio server as `ai.codenib/codenib` in the
official MCP Registry. Registry clients request one required value: the
absolute path to a repository previously indexed with `codenib index`. The
declared launch is equivalent to:

```bash
uvx --with "codenib[mcp]==0.2.0" \
  "codenib==0.2.0" mcp /absolute/path/to/repository
```

The Registry path is intentionally query-only and model-free. It can always
serve the persisted BM25 view without downloading an embedding model. Use the
normal client configuration above from an environment with `semantic` or
`graph` installed when those richer persisted views are required.

## Tools

Only tools whose backing views are fresh and available can return results.

| Tool | Backing view | Granularity | Use for |
|---|---|---|---|
| `search_context` | available `bm25`, `vector`, and `symbol_graph` views | file/symbol | Recommended planned ranked search; reports the selected route and source identity |
| `search_semantic` | `vector` | file/symbol (L0/L2) | Natural-language or conceptual queries |
| `search_bm25` | `bm25` | symbol | Exact names and keyword lookups |
| `search_regex` | `symbol_graph` | file / symbol | Structural pattern matching |
| `search_zoekt` | `zoekt` | file | Fast substring or regex search over files |
| `dependency_subgraph` | `symbol_graph` | call graph | Caller impact, callee dependencies, or a one-hop neighborhood |
| `lsp_definition` | runtime LSP provider or `symbol_graph` fallback | location | Static go-to-definition-shaped lookup |
| `lsp_references` | runtime LSP provider or `symbol_graph` fallback | locations | Static find-references-shaped lookup |
| `lsp_route` | runtime LSP provider or `symbol_graph` fallback | locations | Compact route anchors from symbol seeds or a bounded query fallback |
| `read_source` | verified checkout | source window | Read an exact 1-based span after retrieval or navigation |
| `get_manifest` | manifest | repository | Repository identity, languages, view states, and capabilities |

All source locations returned by MCP use 1-based line numbers. Search tools
reject blank query text, cap it at 16,000 characters, and accept `top_k` values
from 1 through 100.

Call `lsp_route` with `symbols=[]` and a non-blank query when no reliable symbol
is known. This best-effort fallback examines at most 10,000 graph nodes, retains
at most 256 query matches and 512 expanded candidates, and stops after 100
milliseconds.

For a source-verified local C/C++-only checkout, the server can reuse an
existing project-local clangd index for definition and reference symbol
queries. This runtime selection never generates clangd data. Position and route
queries lazily load the compatible complete graph once. Portable artifacts,
mixed-language repositories, unverified checkouts, and disabled or unavailable
native support remain on the persisted symbol graph. LSP result rows identify
their backend, fallback reason, capabilities, and snapshot when provider
metadata is available; `get_manifest.runtime.lsp_provider` reports the same
selection even before a result is returned.

`search_context` accepts `query`, `top_k` (1-100), `budget`
(`fast`, `balanced`, or `thorough`), dense `level` (`l0` or `l2`), and
`filter_test`. Its response separates the selected `plan`, indexed `source`
(repository, commit, and source fingerprint), and ranked `results`. It never
silently labels a sparse fallback as hybrid or graph-expanded execution.
Across all search tools, ranked metadata is retained while source bodies share
a 10,000-character response budget. Projected hits report original and returned
character counts under `content_projection`. Path and symbol fields have
separate limits and report pathological truncation under `metadata_projection`.
`read_source` accepts a
repository-relative POSIX path and a 1-based inclusive range of at most 200
lines; it returns at most 16,000 characters with commit and source-fingerprint
provenance. Source reads remain disabled when startup cannot verify the checkout
against its manifest or portable-artifact binding.

The `codenib-guide` prompt explains how to choose among available tools.
Parameter and return schemas live in
[`codenib/mcp/README.md`](https://github.com/sysevol-ai/CodeNib/blob/main/codenib/mcp/README.md).

## Advanced Views

The `full` preset requests BM25, vectors, a symbol graph, and Zoekt:

```bash
python -m pip install "codenib[full]==0.2.0"
codenib toolchain install /path/to/repository --scope graph
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
