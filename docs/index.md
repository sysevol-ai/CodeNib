---
hide:
  - navigation
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib

CodeNib finds the source code a coding agent needs and serves it with file and
line evidence. It builds several views of one repository snapshot (BM25,
embeddings, a typed SCIP/LSP symbol graph) and offers them three ways: as
fixed retrieval pipelines, as fixed LLM workflows, and as tools inside an
agent loop.

The shipping path for Codex and Claude Code is
[local CodeGraph setup](codegraph.md): model-free, with no API key. The headline
measured result is the [grep → Jev experiment](evaluation/grep_jev.md):
**71.4% code-block Recall@5 on 100 issues**, 12.8 percentage points above
the same grep candidates without reranking. That route is optional and uses
remote models.

## How these docs are organized

| Section | Read it to | Start with |
|------|---------|------------|
| [Get Started](get-started/index.md) | Install CodeNib and connect a repository to your agent | [Agent-ready CodeGraph](codegraph.md) |
| [Guides](guides/index.md) | Serve context over MCP, add grep → Jev, or embed CodeNib in an agent | [MCP Server](mcp.md) |
| [Concepts](concepts/index.md) | Understand the views and how each method uses them | [Context methods and support matrix](concepts/methods.md) |
| [Benchmarks](evaluation/index.md) | See what is measured, on which datasets, and how to reproduce it | [Method × benchmark matrix](evaluation/index.md#method-benchmark-matrix) |
| [API Reference](api/codenib/index.md) | Browse the public Python contracts | `codenib` public surface |
| [Development](development/index.md) | Extend, test, and release CodeNib | [Contributing a Language](contributing-a-language.md) |

Language support differs by view. The generated
[language capability matrix](language_capabilities.md) shows which languages
support chunking, graph indexing, incremental backends, and C++ core decoder
parity.

## Quick Start

CodeNib 0.2.4 can prepare a local, model-free CodeGraph and register it with
installed Codex and Claude Code clients in one command:

```bash
python -m pip install "codenib[graph,mcp]==0.2.4"
codenib codegraph init /path/to/repository
```

The command installs package-managed language providers, builds reusable BM25
and symbol-graph views under `~/.codenib`, and delegates MCP registration to
the clients' own CLIs. The target checkout remains unchanged. Read the
[CodeGraph guide](codegraph.md) for status, safe uninstall, client scopes, and
tool examples.

```bash
python -m pip install "codenib[semantic]==0.2.4"
codenib wiki /path/to/repository
```

The separate Wiki path enables hybrid BM25+dense retrieval with the pinned
CodeRankEmbed model. Read the [Quickstart](quickstart.md) for provider setup and
browser troubleshooting.

## Serving Docs Locally

```bash
git clone https://github.com/sysevol-ai/CodeNib.git
cd CodeNib
python -m pip install -e ".[dev]"
mkdocs serve
```

Then open [http://localhost:8000](http://localhost:8000).
