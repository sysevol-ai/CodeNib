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

Install CodeNib 0.2.5 with the graph runtime and official MCP SDK:

```bash
python -m pip install "codenib[graph,mcp]==0.2.5"
codenib codegraph init /absolute/path/to/repository
```

CodeNib 0.2.5 accepts edited checkouts, rejects stale indexed results after
source changes and reloads explicitly rebuilt indexes within the same MCP
connection.

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

Initialization accepts uncommitted edits, including new, deleted and
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

You can expose a single composed exploration tool to your
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
cost or better answers.

## Claude Code plugin

In Claude Code, the CodeNib plugin replaces the per-repository registration:

```bash
claude plugin marketplace add sysevol-ai/CodeNib
claude plugin install codenib@codenib
```

It needs the `codenib` command (`uv tool install "codenib[graph,mcp]"` or
`pipx install "codenib[graph,mcp]"`); with uv installed and no `codenib` on the
path, it runs CodeNib through `uvx`. In each Git checkout you open, the
plugin's MCP server builds or refreshes the search index and then starts with
`--defer-graph --tool-surface explore`, as described in the next section.
Indexes stay under `~/.codenib`. Outside a Git checkout the server does not
start. The server is started by a Bash script, so Windows needs Git Bash or
WSL.

The plugin also adds four skills:

| Skill | Use |
| --- | --- |
| `/codenib:explain` | Explains how code works, where it lives or what calls it, citing `path:line` |
| `/codenib:impact` | Lists and runs the tests a change can reach (the [change guard](guard.md), Python only) |
| `/codenib:setup` | Installs CodeNib and the language providers, and builds the index and graph |
| `/codenib:wiki` | Opens the local Wiki, or sets up publishing it on GitHub Pages |

Claude may use `explain` and `impact` by itself; `setup` and `wiki` run only
when you invoke them. A repository already registered with
`codenib codegraph init . --agent claude` keeps that server too. To avoid
duplicate tools, remove it with `codenib codegraph uninstall .`.

## Grok Build plugin

Grok Build reads the same plugin, skills and MCP server included. The server
starts in the project Grok opens. Load it from a clone; the `[plugins]` table in
`~/.grok/config.toml` keeps it loaded in every session:

```bash
git clone --depth 1 https://github.com/sysevol-ai/CodeNib ~/.grok/codenib
grok --plugin-dir ~/.grok/codenib/plugins/codenib
```

```toml
[plugins]
paths = ["~/.grok/codenib/plugins/codenib"]
```

It needs the `codenib` command, or uv, as in Claude Code.

## DeepSeek Harness bundle

In [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness), a
bundle adds the same MCP server:

```bash
dsh plugin add "git+https://github.com/sysevol-ai/CodeNib.git#main&path:/plugins/dsh"
```

Replace `main` with a commit to pin what runs. Start `dsh` inside a Git
checkout: the server indexes that checkout, then starts as above. The bundle
starts the server with `sh`, so it loads on macOS and Linux and Windows hosts
skip it. It adds no skills; the server's own instructions tell the model when
to use `explore_context`.

## Prepare the graph when needed

Start with retrieval before graph preparation:

```bash
codenib codegraph init . --defer-graph --tool-surface explore
```

This builds BM25 and registers the agent connection in a few seconds. An
`explore_context` query without symbols starts with ranked, verified source
and never waits for a graph.

For a Python repository, the graph is prepared in the background as soon as
the agent's MCP server starts: CodeNib installs its managed language providers
if needed and builds the graph while the agent reads the task. Indexing runs
in a private copy of the Python files, so the checkout, its `git status` and
concurrent source checks are untouched while it runs.

Provide `symbols` or set `include_dependencies=true` when you need indexed
relationships. If the graph is not ready within about five seconds, the call
answers from source search alone, adds the requested symbol names to that
search, and reports a `graph_preparing` diagnostic; repeat the query later to
get callers and callees. Once a complete graph is ready, the same connection
includes its dependencies by default. Set `include_dependencies=false` to omit
dependency expansion even then. The response plan reports whether
dependencies were enabled. Prebuilt mode keeps its existing default
dependency expansion.

Repositories in other languages prepare the graph on the first request for
relationships, and that request waits for it. On the full tool surface,
dependency, regex-node and LSP queries also wait for the graph, building it
again if none is available.

Preparation retains the normal source checks and refuses partial-language
graphs. Missing project prerequisites remain errors; CodeNib does not install
repository dependencies. If a background build fails, exploration keeps
answering from source and reports a `graph_unavailable` diagnostic with the
reason; run `codenib codegraph init` to retry.

Edits do not stop the server. When source has changed, the next query
re-indexes retrieval for the current checkout (about 1 s for Flask and 5 s for
a 1,200-file Python repository on a developer machine), reloads, and answers
from the edited source. For Python, the graph for the new source is then
prepared in the background as above; other languages prepare it on the next
relationship request. Repeated initialization retains this policy.

Status distinguishes retrieval readiness from full graph readiness. Its
full-readiness check still exits nonzero until both current indexes and
providers are ready. To change the managed policy, uninstall the registration
and initialize again. Include graph preparation and rebuilds in any workflow
comparison: this option moves their cost off the first query and does not by
itself establish a task-efficiency gain.

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
Responses include `semantics` with that scope and the incoming/outgoing
reference direction. Existing
`callers`/`callees` result labels retain their legacy names.
External and dynamic calls may be missing; an empty neighborhood does not prove
there are no callers. Use the returned paths and lines with `read_source` to
check actual invocations and behavior.

CodeNib also recovers some Python references after
`from module import *` when the compiler incorrectly points a name at the
whole module. Recovery requires a position-based definition reply matching an
exact declaration in indexed source. Unsupported targets remain reported in
the build diagnostics. Rebuild an existing Python graph once to pick up this
repair with `codenib codegraph init . --rebuild`. A name that is reassigned can
still reference several static candidates; inspect the source before deciding
which implementation runs.

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

With `--defer-graph`, CodeNib re-indexes retrieval for edited
source on the next query, as described above. Prebuilt registrations reject
indexed queries after source changes: rerun
`codenib codegraph init /absolute/path/to/repository`, then retry in the same
agent connection. The server loads the published indexes after checking that
they match current source. If the compiler is still publishing, retry after it
finishes.

Views whose complete identities remain
current are reused; affected requested views rebuild atomically. File-level
delta repair is currently disabled until it can use the same pinned source
authority. Force a clean graph rebuild only when deliberately changing a
builder or recovering incompatible state:

```bash
codenib codegraph init . --rebuild
```

## Check call-path rules

Use a path rule to flag indexed reference paths that bypass a declared boundary.
Each rule says: every reference path from
an entry point to a `to` symbol passes a `through` symbol, or a function on
the path that also references an `or_calls` symbol. An entry point is a
symbol that nothing else in the checked files references, such as a public
API or a command handler.

Rules live in `.codenib/checks.yaml` in the repository unless `--rules` names
another file:

```yaml
version: 1
rules:
  - id: tool-calls-pass-run-hooks
    message: A tool call must pass the run's before_tool hooks.
    to: [ToolRegistry.perform]
    through: [Loop._call]
    or_calls: [run_guard]
    exclude: [tests, examples]
```

A symbol is a full graph name, a qualified name such as `Loop._call`, or a bare
name; it must match exactly one node, or the command stops and lists the
candidates. `exclude` removes files from the graph before checking, so a
function that only tests call becomes an entry point. A bare directory
excludes its subtree; `*` stays within one path segment and `**` crosses them.

```bash
codenib index /absolute/path/to/repository --preset graph
codenib codegraph check /absolute/path/to/repository
codenib codegraph check . --base origin/main --json
```

Each violation names the caller of the `to` symbol and a shortest path to it
from an entry point that passes no guard:

```text
FAIL tool-calls-pass-run-hooks: A tool call must pass the run's before_tool hooks.
  agent/ext/planning.py:93  Plan.execute() calls ToolRegistry.perform()
      Plan.tools() -> Plan.execute() -> ToolRegistry.perform()
  agent/ext/program.py:217  _Interpreter.expr() calls ToolRegistry.perform()  [unresolved call, matched by name]
      ProgramTools.tools() -> ProgramTools.run() -> _Interpreter.block() -> _Interpreter.expr() ~> ToolRegistry.perform()
```

With `--base`, a violation is listed only when the changes since the merge
base touch its unguarded region or its call; the others are counted. Changes
include uncommitted edits and untracked files, so an agent can reindex and
check its own edit before committing it. Exit status is 0 when every rule
passes, 1 for violations and 2 for an invalid rule, revision or graph. The
command checks the indexed source and refuses a graph that does not match the
checkout, has a partial compiler index, or has an incomplete language report.
Permission to attempt partial indexing does not by itself reject a completed
index. Index the working tree
under review first.

For Python, calls without an unambiguous resolved reference at that line are
also matched by callee name. Several same-named reads on one line retain the
possible unresolved calls; one resolved edge cannot explain them all. The
report marks name-matched hops with `~>` and includes their upstream regions
when filtering a diff, even if the selected witness uses only indexed edges.

These rules describe static references. Passing a method as a callback also
creates an edge. `or_calls` checks whether a function references the named
symbol; it does not establish execution order, branch conditions, or successful
guard execution. Python's dynamic calls can escape name matching, and other
languages use resolved references only. The report states parse failures and
fallback coverage; a passing check does not prove every runtime path is guarded.

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
