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

This site tracks the `0.2` alpha. Install its immutable wheel directly; plain
`pip install codenib` still selects the stable `0.1` line.

```bash
export CODENIB_ALPHA_WHEEL="https://test-files.pythonhosted.org/packages/3d/3d/8e7ce04893c0d64146b96dda6bda448638a00753806f76f7d5cd1e7b1e4d/codenib-0.2.0a1-py3-none-any.whl#sha256=915356bc00e6ae58b1938baf105f79466da4b55ae612a84cc922a3bec09ecb07"
python -m pip install "codenib @ ${CODENIB_ALPHA_WHEEL}"
codenib wiki /path/to/repository
```

The default Wiki is deterministic, uses BM25, and needs no API key. CodeNib
detects repository languages, writes a reusable manifest under
`~/.codenib/repositories`, and opens the browser UI. This default `fast` profile
leaves the target checkout unchanged. Read the
[Quickstart](quickstart.md) for profiles and troubleshooting.

```bash
python -m pip install "codenib[mcp] @ ${CODENIB_ALPHA_WHEEL}"
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
