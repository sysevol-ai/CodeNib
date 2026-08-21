<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent-ready CodeGraph

CodeNib turns a local checkout into a reusable, source-linked graph and makes
that graph available to Codex and Claude Code through MCP. The default path is
local and model-free: it combines ranked BM25 retrieval with SCIP- or
clangd-backed symbol relationships, definitions, references, dependencies, and
bounded source reads.

## One-command setup

Install CodeNib 0.2.2 with the graph runtime and official MCP SDK:

```bash
python -m pip install "codenib[graph,mcp]==0.2.2"
codenib codegraph init /absolute/path/to/repository
```

The initializer detects repository languages and installed agent clients. It
then installs package-managed graph providers, builds `bm25` and
`symbol_graph`, and invokes the native client CLIs:

- Codex receives a user-level stdio MCP registration through `codex mcp add`.
- Claude Code receives a local-scope registration through `claude mcp add` in
  the selected repository.

CodeNib does not edit Codex TOML, Claude JSON, `.mcp.json`, `AGENTS.md`, or
`CLAUDE.md` itself. It never writes an index into the target checkout. A
readable repository slug plus a path digest makes the server name unique, so
several checkouts can coexist.

Initialization requires a clean Git working tree and verifies the same status
again after indexing and native client registration. CodeNib may install its
own pinned language provider below `CODENIB_SCIP_TOOLS_DIR`, but this product
path does not run a repository package manager, generate a compilation
database, or prepare project-local dependencies. If a graph provider needs an
existing `node_modules`, Ruby bundle, or `compile_commands.json`, the command
reports that prerequisite instead of changing the checkout.

When both clients are installed, both are configured. Select one explicitly or
preview the complete plan:

```bash
codenib codegraph init . --agent codex
codenib codegraph init . --agent claude
codenib codegraph init . --agent codex --agent claude --dry-run
```

Running the same initialization again reuses a current index and matching
native registrations. CodeNib refuses to overwrite an unmanaged server with
the same name or a managed registration whose command has drifted.

## Select the repository source surface

Generated or vendored subtrees can be excluded with a repeatable, exact
repository-relative path:

```bash
codenib codegraph init . \
  --exclude-dir ios/Pods \
  --exclude-dir generated/api
```

An explicit set replaces the complete custom exclusion policy. With no flag,
later `codegraph init` and `index` runs reuse the policy recorded in the
repository manifest. Clear it explicitly when those trees should become source
again:

```bash
codenib codegraph init . --clear-exclude-dirs
```

Paths use repository-relative POSIX spelling (`/`) on every platform and do
not accept glob syntax. The match is lexical and component-aware:
`ios/Pods` excludes that exact
subtree, not `packages/mobile/ios/Pods` or a directory with a similar prefix.
CodeNib applies the same selection to language detection, source identity,
BM25/vector documents, graph/SCIP output, runtime verification, and status.
Changing it therefore rebuilds affected views instead of relabeling an old
artifact as current.

CodeNib does not implicitly consume `.gitignore`, `.git/info/exclude`, or a
global Git excludes file as its source policy. Ignored and untracked local
source can be meaningful input, while ambient global rules would make one
manifest mean different things on different machines.

Zoekt requires its fixed commit tree to match the authenticated checkout and
contain no tracked path rejected by the default repository policy. It cannot
yet prove a non-empty custom selection end to end. Requests for the `zoekt`
view, including `--preset full`, therefore fail before producing a new shard
when either condition is unmet. The default CodeGraph path uses BM25 plus
`symbol_graph` and remains supported.
Serving an authenticated Zoekt shard through MCP is Linux-only in 0.2.2 because
the child process receives a retained `/proc` descriptor path rather than the
mutable published directory.

Absolute symlinks are accepted only by the authenticated indexing path when
their target remains inside the same pinned checkout. The target is re-walked
from that checkout and retains the normal identity and rebind checks; links to
another directory, device paths, and prefix lookalikes remain rejected.

## Use it from an agent

Start broad questions with `explore_context`. It composes ranked retrieval,
symbol routing, dependency expansion, and verified source windows under one
response budget:

> Use CodeNib's `explore_context` to explain where request retries are
> implemented. Cite the returned source paths and lines before proposing an
> edit.

Use `dependency_subgraph` for structural questions that keyword search cannot
answer:

> Use `dependency_subgraph` on `RetryPolicy.execute` with direction `impact`
> and depth 2. Summarize callers that could change if its behavior changes.

Useful full-surface tools include:

| Tool | Product use |
| --- | --- |
| `explore_context` | Bounded, source-verified context for a repository question |
| `dependency_subgraph` | Callers, callees, and one-hop dependency neighborhoods |
| `search_bm25` | Exact identifiers and keywords |
| `search_regex` | Patterns over CodeGraph file and symbol nodes |
| `lsp_definition` / `lsp_references` | Static navigation backed by the graph or a verified live provider |
| `read_source` | Exact 1-based source windows after retrieval or navigation |

The server checks the live checkout against the indexed source identity when
it starts. It does not silently serve a graph for changed source.

## Status and updates

The human report checks the toolchain, current source identity, both graph
views, the CodeNib receipt, and every managed native registration:

```bash
codenib codegraph status /absolute/path/to/repository
```

Automation can use the same contract as JSON. Exit status is zero only when
the complete path is ready:

```bash
codenib codegraph status /absolute/path/to/repository --json
```

After source changes, rerun `init`. Views whose complete identities remain
current are reused; affected requested views rebuild atomically. File-level
delta repair is currently disabled until it can use the same pinned source
authority. Force a clean graph rebuild only when deliberately changing a
builder or recovering incompatible state:

```bash
codenib codegraph init . --rebuild
```

## Safe uninstall

Remove the client registrations without deleting the reusable index:

```bash
codenib codegraph uninstall /absolute/path/to/repository
```

CodeNib removes only clients named in its private per-repository receipt. It
first asks the native CLI for the current configuration and refuses removal if
the command differs. Inspect that registration before explicitly overriding
the guard:

```bash
codenib codegraph uninstall . --agent codex --dry-run
codenib codegraph uninstall . --agent codex --force
```

The `--force` flag affects only a receipt-owned server name. It does not remove
unmanaged MCP entries, source files, graph providers, or indexes.

## Language prerequisites

CodeNib installs pinned npm, Go, Rustup, .NET, and RubyGem providers when their
host package manager is available. It does not invoke `sudo` or silently
prepare project-local dependencies. The initializer reports prerequisites such
as clangd, a JDK, `compile_commands.json`, or project-local PHP tooling and
stops before publishing an incomplete agent setup.

Use the lower-level commands when diagnosing a language:

```bash
codenib toolchain status . --scope graph
codenib doctor . --require graph
```

See [SCIP And Graph Indexing](scip_index.md) and the generated
[Language Capabilities](language_capabilities.md) matrix for the current
provider boundary.

## Troubleshooting

- **No supported client found:** install Codex or Claude Code, confirm its CLI
  is on `PATH`, then rerun `init` with `--agent codex` or `--agent claude`.
- **Missing Python runtime:** install the exact `codenib[graph,mcp]` extra in
  the environment that provides the `codenib` command.
- **Manual graph prerequisite:** follow the reported provider instruction, run
  `codenib doctor . --require graph`, then rerun initialization.
- **Dirty checkout:** commit or stash tracked and untracked files. CodeNib
  checks again after each mutating phase and stops before reporting readiness
  if a provider or client changes the checkout.
- **Source identity mismatch:** rerun `codenib codegraph init .` for the current
  checkout rather than serving a stale graph.
- **Generated subtree should be omitted:** rerun `init` with one or more exact
  `--exclude-dir PATH` values. The values replace the persisted custom set;
  inspect them with `codegraph status` before serving the graph.
- **Configuration differs:** inspect the named server with `codex mcp get` or
  `claude mcp get`. CodeNib will not overwrite or remove drift implicitly.

Set `CODENIB_HOME` to relocate indexes and the management receipt. Native
client configuration remains in the location controlled by that client.
