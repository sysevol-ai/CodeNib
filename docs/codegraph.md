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

Use it for repeated definition, reference and cross-file navigation questions.
Your agent's native text search and file reads can handle a known string or
file directly. Include initial indexing and later rebuilds when assessing
whether graph navigation helps your own workflow.

## One-command setup

Install CodeNib 0.2.4 with the graph runtime and official MCP SDK:

```bash
python -m pip install "codenib[graph,mcp]==0.2.4"
codenib codegraph init /absolute/path/to/repository
```

Published 0.2.4 requires a clean Git checkout for initialization. Source builds
from `main` add dirty-checkout initialization and per-query stale-index
rejection, then reload rebuilt indexes within the same MCP connection. These
fixes await a package release.

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

On `main`, initialization accepts uncommitted edits, including new, deleted and
renamed files. It indexes the current working tree and checks that visible source
content, the Git commit and status remain unchanged through toolchain setup,
indexing and native client registration. CodeNib may install its
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

Source builds also let you expose a single composed exploration tool to your
agent:

```bash
codenib codegraph init . --tool-surface explore
```

This mode exposes `explore_context`; choose `--tool-surface full` for separate
search, dependency, source-read and LSP tools. In this single-entry mode,
CodeNib asks supporting Claude Code clients to load the entry upfront. Client
settings can override that request; the agent still chooses whether to call
it. See [Claude Code's tool-discovery controls](https://code.claude.com/docs/en/mcp#mark-a-tool-to-load-upfront-or-stay-deferred).
New registrations use `full` by
default. Repeated initialization retains the existing selection. To change
it, run `codenib codegraph uninstall .` before initializing again. The tool
selection does not change which indexes are prepared; the smaller list does not establish lower task
cost or better answers. This option awaits a package release.

## Prepare the graph when needed

Source builds support retrieval before graph preparation:

```bash
codenib codegraph init . --defer-graph --tool-surface explore
```

This builds BM25 and registers the agent connection without preparing language
providers or building a graph. For ranked source lookup, call `explore_context`
with `include_dependencies=false` and no `symbols`. Providing symbols or
requesting dependencies prepares the graph on that first query. On the full
tool surface, dependency, regex-node and LSP queries also trigger preparation.
Once prepared, the same connection reuses the graph.

The first graph query may install CodeNib's managed providers and wait for
indexing. It retains the normal source checks and refuses partial-language
graphs. Missing project prerequisites remain errors; CodeNib does not install
repository dependencies. A cancelled request may finish its local compilation;
its response is discarded and a retry waits for that worker. Ordinary BM25
retrieval remains available after a provider failure if source is unchanged.

Repeated initialization retains this policy and refreshes BM25 after edits;
the next graph query pays the graph update cost. Status distinguishes retrieval
readiness from full graph readiness. Its full-readiness check still exits
nonzero until both current indexes and providers are ready. To change the
managed policy, uninstall the registration and initialize again. Include the
first graph query and subsequent rebuilds in any workflow comparison: this
option defers their cost and does not establish a task-efficiency gain. It
awaits a package release.

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

Choose a question from the repository you are working on:

| What you need | Start with |
| --- | --- |
| A known file, error string or identifier | Native file reads or text search |
| A definition or its reference locations | `lsp_definition` or `lsp_references`, then inspect the source |
| Related code for a broader question | `explore_context`, then follow relevant static references |

For a first connection check, explicitly request `explore_context`. It composes
ranked retrieval, symbol routing, dependency expansion and verified source
windows under one response budget. This example assumes your repository
implements request retries:

> Use CodeNib's `explore_context` to explain where request retries are
> implemented. Cite the returned source paths and lines before proposing an
> edit.

Use a symbol returned by that lookup to follow indexed static references:

> Use `dependency_subgraph` on the symbol you found, with direction `impact`
> and depth 2. Summarize incoming references and inspect their source call sites
> before explaining which functions actually invoke it.

References can come from calls, fields, type annotations or function values.
Source builds from `main` add `semantics` with that scope and the incoming/outgoing
reference direction. Published 0.2.4 keeps the older result format. Existing
`callers`/`callees` result labels retain their legacy names.
External and dynamic calls may be missing; an empty neighborhood does not prove
there are no callers. Use the returned paths and lines with `read_source` to
check actual invocations and behavior.

Useful full-surface tools include:

| Tool | Product use |
| --- | --- |
| `explore_context` | Bounded, source-verified context for a repository question |
| `dependency_subgraph` | Bounded incoming/outgoing static-reference neighborhoods |
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

Source builds from `main` reject indexed queries after source changes. Rerun
`codenib codegraph init /absolute/path/to/repository`, then retry in the same
agent connection. The server loads the published indexes after checking that
they match current source. If the compiler is still publishing, retry after it
finishes. Queries do not start an index build.

Published 0.2.4 requires a clean checkout for rebuilding. Stop its CodeNib MCP
server, rerun `init`, and restart the server to load the result. A running
0.2.4 session can return stale facts after an edit.

Views whose complete identities remain
current are reused; affected requested views rebuild atomically. File-level
delta repair is currently disabled until it can use the same pinned source
authority. Force a clean graph rebuild only when deliberately changing a
builder or recovering incompatible state:

```bash
codenib codegraph init . --rebuild
```

## Recorded agent session

The homepage's 15-second clip replays actual CLI output from
[Requests v2.32.5](https://github.com/psf/requests/tree/b25c87d7cb8d6a18a37fa12442b5f883f9e41741).
It shows `codenib codegraph init . --agent claude`, then a real Claude Code
session that makes two calls. `explore_context` answers a plain-language
question about authentication during redirects with source anchors at
[`rebuild_auth`, lines 282–300](https://github.com/psf/requests/blob/b25c87d7cb8d6a18a37fa12442b5f883f9e41741/src/requests/sessions.py#L282-L300)
and
[`should_strip_auth`, lines 127–157](https://github.com/psf/requests/blob/b25c87d7cb8d6a18a37fa12442b5f883f9e41741/src/requests/sessions.py#L127-L157).
One `dependency_subgraph` call with `direction="impact"` and depth 4 then
returns the indexed reference chain `Session.request()` → `Session.send()` →
`resolve_redirects()` → `rebuild_auth()`, plus the entry points
`requests.request()` in `api.py` and seven `Session` HTTP-verb methods.
This is a selected, source-checkable path; it is not a complete runtime trace.

The recording runs CodeNib main commit
[`f7060886`](https://github.com/sysevol-ai/CodeNib/commit/f70608869ff4fff982a43da8f6a776fd9decbc18)
from a source checkout in an existing `[graph,mcp]` environment with an
installed `scip-python`. Client registration and index state used an isolated
home directory. The agent call took 14.1 seconds on this one run.
Installation and provider setup are outside the clip; waits are condensed.
This is an edited CLI transcript, not a recording of Claude's interactive UI,
a cold-start benchmark or evidence of token savings.

Claude Code 2.1.284 uses `anthropic/claude-sonnet-4.6` through
[OpenRouter's documented gateway](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration).
The local CodeGraph route makes no model call; the agent sends its prompt and
returned source to its model. The run allowed only the two CodeNib tools,
disabled built-in tools, kept no session, and set a four-turn limit and a
$0.30 application budget. It used three turns. Source and existing user
profiles stayed unchanged. Claude's $0.0778 cost estimate uses list prices;
it is not a provider billing receipt.

The [recorded transcript and metadata](https://github.com/sysevol-ai/CodeNib/blob/main/landing/assets/demos/codegraph-claude.json)
retain the exact prompt, tool inputs, selected anchors, the dependency result,
the full answer, usage and source identities. Every cited line is a definition
line at the pinned source; the `Authorization` header is deleted at line 295.
Regenerate the media without model calls using
`node scripts/render_agent_demo.mjs` after installing the Web development
dependencies, Playwright Chromium and ffmpeg.

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
- **Checkout changed during initialization:** inspect `git diff` and
  `git status`. Initialization accepts existing edits, but stops if visible
  source, the Git commit or status changes during setup. Retry after the
  concurrent work has finished.
- **Source identity mismatch:** rerun `codenib codegraph init .` for the current
  checkout rather than serving a stale graph.
- **Containment cycle:** the language provider has reported an impossible
  source hierarchy. SCIP Python 0.6.6 can merge an enclosing method and a
  same-named nested function ([#871](https://github.com/sysevol-ai/CodeNib/issues/871)).
  CodeNib rejects that graph. Use source search while definition identity is
  unresolved; BM25 indexing can still succeed independently of the graph.
- **Generated subtree should be omitted:** rerun `init` with one or more exact
  `--exclude-dir PATH` values. The values replace the persisted custom set;
  inspect them with `codegraph status` before serving the graph.
- **Configuration differs:** inspect the named server with `codex mcp get` or
  `claude mcp get`. CodeNib will not overwrite or remove drift implicitly.

Set `CODENIB_HOME` to relocate indexes and the management receipt. Native
client configuration remains in the location controlled by that client.
