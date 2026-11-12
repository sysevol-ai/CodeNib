<!--
SPDX-FileCopyrightText: 2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib for Claude Code, Codex and Grok Build

Ask about your repository and get answers tied to the exact lines. The plugin
connects Claude Code, Codex or Grok Build to CodeNib, which ranks source for a
question and follows the calls and references that compiler-grade indexers
recorded between files.

```bash
claude plugin marketplace add sysevol-ai/CodeNib
claude plugin install codenib@codenib
```

Codex installs the same plugin:

```bash
codex plugin marketplace add sysevol-ai/CodeNib --sparse .claude-plugin --sparse plugins/codenib
codex plugin add codenib@codenib
```

Grok Build reads it too:

```bash
grok plugin install "sysevol-ai/CodeNib#plugins/codenib"
```

CodeNib itself is a Python package (3.10 or later). Install it once, or let
the plugin run it through `uvx` if you have uv:

```bash
uv tool install "codenib[graph,mcp]"     # or: pipx install "codenib[graph,mcp]"
```

The plugin needs CodeNib 0.2.5 or later. It passes over an older `codenib` on
your path and uses `uvx`; without uv, it tells you to upgrade.

## What it adds

| Component | What it does |
| --- | --- |
| `plugin:codenib:codenib` MCP server | `explore_context`: ranked, verified source windows for a question, plus recorded references once the code graph is ready |
| `/codenib:explain` | Answers how code works, where it lives, or what calls it, citing `path:line` for each claim |
| `/codenib:impact` | Lists the tests a change can reach, with call paths, and runs them (preview, Python only) |
| `/codenib:thread` | Reads a GitHub issue or pull request in one step: discussion, review comments, failing checks, linked threads and the code they point at (preview, CodeNib 0.2.6) |
| `/codenib:setup` | Installs CodeNib and the language providers, and builds the search index and code graph |
| `/codenib:wiki` | Opens the repository's Wiki locally, or sets up publishing it on GitHub Pages |

The agent uses `explain`, `impact` and `thread` on its own when a question
fits; `setup` and `wiki` run only when you ask. In Codex the skills are named
`codenib:explain` and so on.

## Read an issue or pull request in one step

An agent that reads a GitHub thread itself spends a tool call on each piece:
the issue, its comments, the linked pull request, the reviews, the checks, a
log. `/codenib:thread 812` runs `codenib threads show 812`, which prepares all
of it once, with no model, inside a token budget. When the agent returns to
the thread, it gets only what changed.

On 76 pull requests taken over after a review or a failed check, the prepared
context cut Claude Code's input tokens by 45% against reading with `gh`. It
found the files to change about as often (0.80 against 0.83, a gap too small
to tell from chance at that size). See
[Thread context](https://docs.codenib.ai/threads/) for the method and the
commands for following many threads at once.

## What happens when it starts

In each Git checkout you open, the MCP server first builds or refreshes
CodeNib's search index, then starts. Indexes are stored under `~/.codenib`;
nothing is written into the checkout. For Python, the code graph is prepared
in the background as soon as the server starts; for other languages, on the
first question that needs references. Run `/codenib:setup` to build the graph
ahead of time. Outside a Git checkout, the server does not start.

The server and its index run locally and call no model. `/codenib:wiki` calls a
model only when you choose model-written pages and give it a key.

## Network and data

The MCP server reads the checkout and writes CodeNib's index under
`~/.codenib`. It calls no model and needs no key. If `codenib` is not
installed, the plugin runs it through `uvx`, which downloads the `codenib`
package from PyPI. `/codenib:setup` installs CodeNib and language indexers only
when you run it. `/codenib:wiki` sends source to a model provider only when you
choose model-written pages and give it a key. `/codenib:thread` reads the
issue or pull request from GitHub with your `GH_TOKEN` or signed-in `gh`, and
keeps what it read under `~/.codenib/threads`. What people wrote in the thread
reaches the agent, as it would if the agent read GitHub itself. The plugin has
no hooks.

The server is started by a Bash script, so on Windows it needs Git Bash or WSL.
See the [CodeGraph guide](https://docs.codenib.ai/codegraph/) for the tools,
their limits, and troubleshooting.
