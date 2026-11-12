<!--
SPDX-FileCopyrightText: 2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib for DeepSeek Harness

Ask about your repository and get answers tied to the exact lines. This bundle
connects [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
to CodeNib, which ranks source for a question and follows the calls and
references that compiler-grade indexers recorded between files.

```bash
dsh plugin add "git+https://github.com/sysevol-ai/CodeNib.git#main&path:/plugins/dsh"
```

Replace `main` with a commit to pin what runs. CodeNib itself is a Python
package (3.10 or later). Install it once, or let the bundle run it through
`uvx` if you have uv:

```bash
uv tool install "codenib[graph,mcp]"     # or: pipx install "codenib[graph,mcp]"
```

The bundle needs CodeNib 0.2.5 or later. It passes over an older `codenib` on
your path and uses `uvx`; without uv, it tells you to upgrade.

## What it adds

The bundle adds one MCP server, `codenib`. Its `explore_context` tool returns
ranked, verified source windows for a question, plus recorded references once
the code graph is ready; the model sees it as `mcp__codenib__explore_context`.

## What happens when it starts

Start `dsh` inside a Git checkout. The server first builds or refreshes
CodeNib's search index for that checkout, then starts. Indexes are stored
under `~/.codenib`; nothing is written into the checkout. For Python, the code
graph is prepared in the background as soon as the server starts; for other
languages, on the first question that needs references. Run
`codenib index . --preset graph` in the checkout to build the graph ahead of
time. Outside a Git checkout, the server does not start and dsh runs without
it.

The server and its index run locally and call no model. Running CodeNib
through `uvx` downloads it from PyPI.

The server is started with `sh`, so the bundle loads on macOS and Linux;
Windows hosts skip it. See the [CodeGraph guide](https://docs.codenib.ai/codegraph/)
for the tools, their limits, and troubleshooting.
