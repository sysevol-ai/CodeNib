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
| Serve repository context to coding agents | [Guides](guides/index.md) | [MCP Server](mcp.md) |
| Understand indexes, graphs, and incremental updates | [Concepts](concepts/index.md) | [Incremental Graph](incremental_graph/index.md) |
| Browse source-generated Python contracts | [API Reference](api/codenib/index.md) | `codenib` public surface |
| Extend, test, release, or evaluate CodeNib | [Development](development/index.md) | [Contributing a Language](contributing-a-language.md) |

## Quick Start

This site tracks the `0.2` alpha. The recommended local install enables hybrid
BM25+dense retrieval with the pinned CodeRankEmbed model.

```bash
python -m pip install "codenib[semantic]==0.2.0"
codenib wiki /path/to/repository
```

The CLI detects the installed semantic capability, writes a reusable manifest
under `~/.codenib/repositories`, and opens the browser UI. Install
`codenib==0.2.0` without extras for the smaller no-model BM25 fallback. Both
paths leave the target checkout unchanged. Read the [Quickstart](quickstart.md)
for provider setup and troubleshooting.

```bash
python -m pip install "codenib[mcp,semantic]==0.2.0"
codenib mcp /path/to/repository
```

## Serving Docs Locally

```bash
git clone https://github.com/sysevol-ai/CodeNib.git
cd CodeNib
python -m pip install -e ".[dev]"
mkdocs serve
```

Then open [http://localhost:8000](http://localhost:8000).
