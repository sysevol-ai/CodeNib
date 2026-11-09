<!--
SPDX-FileCopyrightText: 2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib for Claude Code

Ask about your repository and get answers tied to the exact lines. The plugin
connects Claude Code to CodeNib, which ranks source for a question and follows
the calls and references that compiler-grade indexers recorded between files.

```bash
claude plugin marketplace add sysevol-ai/CodeNib
claude plugin install codenib@codenib
```

CodeNib itself is a Python package (3.10 or later). Install it once, or let
the plugin run it through `uvx` if you have uv:

```bash
uv tool install "codenib[graph,mcp]"     # or: pipx install "codenib[graph,mcp]"
```

## What it adds

| Component | What it does |
| --- | --- |
| `plugin:codenib:codenib` MCP server | `explore_context`: ranked, verified source windows for a question, plus recorded references once the code graph is ready |
| `/codenib:explain` | Answers how code works, where it lives, or what calls it, citing `path:line` for each claim |
| `/codenib:impact` | Lists the tests a change can reach, with call paths, and runs them (preview, Python only) |
| `/codenib:setup` | Installs CodeNib and the language providers, and builds the search index and code graph |
| `/codenib:wiki` | Opens the repository's Wiki locally, or sets up publishing it on GitHub Pages |

Claude uses `explain` and `impact` on its own when a question fits; `setup` and
`wiki` run only when you ask.

## What happens when it starts

In each Git checkout you open, the MCP server first builds or refreshes
CodeNib's search index, then starts. Indexes are stored under `~/.codenib`;
nothing is written into the checkout. For Python, the code graph is prepared
in the background as soon as the server starts; for other languages, on the
first question that needs references. Run `/codenib:setup` to build the graph
ahead of time. Outside a Git checkout, the server does not start.

The server and its index run locally and call no model. `/codenib:wiki` calls a
model only when you choose model-written pages and give it a key.

The server is started by a Bash script, so on Windows it needs Git Bash or WSL.
See the [CodeGraph guide](https://docs.codenib.ai/codegraph/) for the tools,
their limits, and troubleshooting.
