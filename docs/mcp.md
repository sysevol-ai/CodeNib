<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# MCP Server

CodeNib serves a pre-built repository manifest to coding agents over
[Model Context Protocol](https://modelcontextprotocol.io/) stdio. Index
construction and query serving are separate: build or update a repository once,
then reuse that manifest across agent sessions.

For the unreleased source-checkout route that searches directly with your
OpenRouter account, see [grep and Jev](guides/grep-jev.md). It exposes
`explore_context` without an index, embedding model, GPU, or graph build.

## Install And Index

For Codex or Claude Code, the recommended 0.2.4 path prepares the graph index
and native client configuration together:

```bash
python -m pip install "codenib[graph,mcp]==0.2.4"
codenib codegraph init /path/to/repository
```

This keeps the checkout clean and uses each client CLI instead of editing its
configuration directly. It builds the `bm25` and `symbol_graph` views and
registers the full tool surface. `explore_context`, `search_context`,
`search_bm25`, `search_regex`, `dependency_subgraph`, the three LSP tools,
`read_source`, and `get_manifest` work. `search_semantic` and `search_zoekt`
stay listed but report their views as unavailable until you build `vector` or
`zoekt`. See [Agent-ready CodeGraph](codegraph.md) for the complete
one-command lifecycle.

For a custom MCP client or a retrieval-only setup, build and launch the server
manually. The smaller no-model fallback is:

```bash
python -m pip install "codenib[mcp]==0.2.4"
codenib index /path/to/repository --preset fast
```

Add static navigation and dependency tools without an embedding download:

```bash
python -m pip install "codenib[graph,mcp]==0.2.4"
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

A portable artifact serves only its `bm25` and `vector` views. The graph,
regex, dependency, and LSP tools are unavailable in artifact mode; index the
checkout locally when you need them.

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

### Tool Surfaces

The default `--tool-surface full` preserves the complete, compatible tool list
and accepts existing calls unchanged. The bounded exploration surface is
selected explicitly:

```bash
codenib mcp /path/to/repository --tool-surface explore
```

With `--tool-surface explore`, the server lists and accepts only
`explore_context`; direct calls to tools hidden by that surface are rejected.
Each stdio connection has its own runtime state, so reconnecting starts a fresh
exploration ledger.

### Retrieval Routes

The default `--retrieval-route indexed` serves the views in the repository's
manifest. `--retrieval-route grep-jev` needs no index: it plans ripgrep
searches with an OpenRouter model over the live checkout and ranks the matches
with Jev. That route always uses the `explore` surface. See
[Search with grep and Jev](guides/grep-jev.md) for credentials, cost limits,
and what leaves your machine.

### MCP Registry

CodeNib publishes its local stdio server as `ai.codenib/codenib` in the
official MCP Registry. Registry clients request one required value: the
absolute path to a repository previously indexed with `codenib index`. The
declared launch is equivalent to:

```bash
uvx --with "codenib[mcp]==0.2.4" \
  "codenib==0.2.4" mcp /absolute/path/to/repository
```

The Registry path is intentionally query-only and model-free. It can always
serve the persisted BM25 view without downloading an embedding model. Use the
normal client configuration above from an environment with `semantic` or
`graph` installed when those richer persisted views are required.

## Tools

Only tools whose backing views are fresh and available can return results.

| Tool | Backing view | Granularity | Use for |
|---|---|---|---|
| `explore_context` | available retrieval, LSP route, `symbol_graph`, and verified checkout | grouped source windows | Bounded retrieval, navigation, dependencies, and source in one call |
| `search_context` | available `bm25`, `vector`, and `symbol_graph` views | file/symbol | Recommended planned ranked search; reports the selected route and source identity |
| `search_semantic` | `vector` | file/symbol (L0/L2) | Natural-language or conceptual queries |
| `search_bm25` | `bm25` | symbol | Exact names and keyword lookups |
| `search_regex` | `regex_index`, derived from `symbol_graph` at load | file / symbol | Structural pattern matching |
| `search_zoekt` | `zoekt` | file | Fast substring or regex search over files |
| `dependency_subgraph` | `symbol_graph` | static-reference subgraph | Bounded incoming/outgoing references across symbols |
| `lsp_definition` | runtime LSP provider or `symbol_graph` fallback | location | Static go-to-definition-shaped lookup |
| `lsp_references` | runtime LSP provider or `symbol_graph` fallback | locations | Static find-references-shaped lookup |
| `lsp_route` | runtime LSP provider or `symbol_graph` fallback | locations | Compact route anchors from symbol seeds or a bounded query fallback |
| `read_source` | verified checkout | source window | Read an exact 1-based span after retrieval or navigation |
| `get_manifest` | manifest | repository | Repository identity, languages, view states, and capabilities |

All source locations returned by MCP use 1-based line numbers. Search tools
reject blank query text, cap it at 16,000 characters, and accept `top_k` values
from 1 through 100.

For `dependency_subgraph`, `impact` follows incoming references,
`dependencies` follows outgoing references, and `both` follows both directions.
References may represent calls, fields, types or function values. Read the
source before describing an invocation or runtime path; external and dynamic
relationships may be missing. Source builds from `main` include `semantics`
with this scope and direction; published 0.2.4 retains the older response shape
and legacy `callers`/`callees` labels. See [CodeGraph](codegraph.md) for the
release and source-update boundaries.

Use `explore_context` to find implementation code and source evidence for a
repository question. Start with `query` alone, including any known symbol names,
error text, or file hints:

```json
{"query": "Where is retry backoff implemented?"}
```

In on-demand graph mode, a cold query without `symbols` starts with source.
When you need indexed relationships, supply `symbols` or
`include_dependencies=true`; preparation can install providers and build the
graph. Ordinary queries reuse a complete warm graph. Set
`include_dependencies=false` to omit expansion explicitly. Prebuilt mode
keeps dependencies enabled by default.

An unreleased source build can answer an exact Python `lsp_definition` request
before building the graph. Use a verified local Python-only repository, the
full tool surface, and locally prepared LSP tools:

```bash
codenib index /path/to/repository --preset fast
codenib toolchain install /path/to/repository --scope lsp
codenib mcp /path/to/repository --defer-graph --tool-surface full
```

Supply `file_path`, a 1-based `line`, and a zero-based UTF-8 byte `character`.
For example, `{"file_path":"app.py","line":12,"character":8}` locates the
token at that position. The source provider returns a candidate only when one
LSP location matches an exact function or class declaration in captured source.
Read its body with `read_source` before drawing conclusions. A declaration
candidate does not establish a unique runtime call target.

This path captures all selected files and prepares all selected Python files;
startup still has a preparation cost. Ambiguous or unsupported positions,
missing software, symbol lookups, references and routes use the existing
complete-graph preparation. An explicitly configured `CODENIB_PYTHON_LSP_CMD`
keeps its existing provider route. Portable artifacts and the `explore` tool
surface do not expose this shortcut. Published 0.2.4 does not include it.
After a source edit, rebuild explicitly; the same MCP connection then reloads
the new source generation. Changed source is never silently rebuilt by a query.

Optional controls bound returned context (`top_k` and a
`fast`/`balanced`/`thorough` budget), select reference direction, or filter test
files. The response identifies the route provider and source binding.
Missing or failed retrieval, route,
dependency, or source providers degrade independently and add bounded
`diagnostics`; unverified indexed excerpts remain marked `verified: false`
instead of being presented as checkout source.

When no explicit symbol seeds are supplied, code-shaped or backtick-quoted
identifiers in the query (including calls such as `decode()`) may become seeds
after an exact match with a repository definition. The route plan reports
concrete names as `inferred_symbols`, including distinct definitions of an
ambiguous leaf name. Inferred routes return definitions; the bounded dependency
phase supplies related symbols. Only requested definitions with verified source
receive priority over ranked hits. Other route neighbors and unverified locations
retain the ranked order. Symbol inference checks at most 32 distinct candidates
and retains at most 32 concrete seeds sharing a 16,000-character aggregate
text bound.

Focused source selection scores only matches that fit its final per-context
delivery allowance, including the composed response budget. Its projection marks omitted scope lines as truncated, even when the
selected text fits the character cap. `original_chars` and `returned_chars`
describe character projection of the selected window; `selection` records its
original scope and omitted lines.

Each complete serialized `explore_context` MCP `CallToolResult` has a hard
256 KiB (262,144-byte) ceiling. Accounting includes the structured and text
forms, and payload projection reserves room for the result/protocol envelope.
The limit therefore does not grant 256 KiB to source content alone.

Within each independent stdio connection, the runtime ledger remembers at most
160 verified source ranges. A repeated identical range may use a stable
`source_call` pointer to the call that supplied its body; unverified indexed
excerpts are not deduplicated. Response summaries expose current ledger usage,
deduplication, and eviction counts.

Call `lsp_route` with `symbols=[]` and a non-blank query when no reliable symbol
is known. This best-effort fallback examines at most 10,000 graph nodes, retains
at most 256 query matches and 512 expanded candidates, and stops after 100
milliseconds.

Since 0.2.2, every manifest-bound C/C++ checkout uses the verified persisted
symbol graph. Project-local clangd `.idx` files often live below the
default-excluded `build/` tree and do not yet carry an authenticated generation
receipt plus allowed-file proof, so the server does not reuse them directly.
This is a fail-closed query-time boundary, not a removal of C/C++ graph
indexing. LSP result rows identify the backend, fallback reason, capabilities,
and snapshot when provider metadata is available;
`get_manifest.runtime.lsp_provider` reports the same persisted-graph selection
before a result is returned.

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
python -m pip install "codenib[full]==0.2.4"
codenib toolchain install /path/to/repository --scope graph
codenib index /path/to/repository --preset full
```

This succeeds only with the default source selection, an immutable commit tree
that exactly matches the authenticated checkout, and no tracked path rejected
by the default policy. A non-empty custom exclusion set, or a tracked
default-excluded path, makes the Zoekt build fail closed; see
[the CodeGraph source-surface boundary](codegraph.md#select-the-repository-source-surface).

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

Authenticated `search_zoekt` serving is Linux-only. The runtime keeps
the verified shard generation open and gives `zoekt-webserver` a `/proc`
descriptor path; macOS and Windows fail closed rather than reopen the mutable
published directory.

See [SCIP Indexing](scip_index.md) and
[Language Capabilities](language_capabilities.md) for backend-specific setup
and support boundaries.
