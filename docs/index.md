---
hide:
  - navigation
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib

CodeNib is a multi-view data system for serving repository context to coding
agents. It compiles repositories into reusable, source-linked lexical, semantic,
structural, and static-navigation views, then serves the same manifest through
MCP, Python APIs, the local Wiki, and evaluation harnesses.

Language support varies by surface. Start with the generated
[Language Capabilities](language_capabilities.md) matrix when you need to know
which languages support chunking, graph indexing, incremental patching, or C++
core decoder parity.

## Choose a path

| Goal | Section | Start with |
|------|---------|------------|
| Install CodeNib and explore a repository | [Get Started](get-started/index.md) | [Quickstart](quickstart.md) |
| Give Codex or Claude Code a repository CodeGraph | [CodeGraph](codegraph.md) | `codenib codegraph init .` |
| Serve custom repository context over MCP | [Guides](guides/index.md) | [MCP Server](mcp.md) |
| Understand indexes, graphs, and incremental updates | [Concepts](concepts/index.md) | [Incremental Graph](incremental_graph/index.md) |
| Browse source-generated Python contracts | [API Reference](api/codenib/index.md) | `codenib` public surface |
| Extend, test, release, or evaluate CodeNib | [Development](development/index.md) | [Contributing a Language](contributing-a-language.md) |

## Quick Start

CodeNib 0.2.1 can prepare a local, model-free CodeGraph and register it with
installed Codex and Claude Code clients in one command:

```bash
python -m pip install "codenib[graph,mcp]==0.2.1"
codenib codegraph init /path/to/repository
```

The command installs package-managed language providers, builds reusable BM25
and symbol-graph views under `~/.codenib`, and delegates MCP registration to
the clients' own CLIs. The target checkout remains unchanged. Read the
[CodeGraph guide](codegraph.md) for status, safe uninstall, client scopes, and
tool examples.

```bash
python -m pip install "codenib[semantic]==0.2.1"
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
