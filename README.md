<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeMiner

**Graph-enhanced code retrieval for LLM agents** — LSP-precise symbol graphs, hybrid search, and a Model Context Protocol server, across six languages.

[![CI](https://github.com/sysevol-ai/CodeMiner/actions/workflows/ci.yml/badge.svg)](https://github.com/sysevol-ai/CodeMiner/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-526CFE.svg)](docs/index.md)

CodeMiner parses a codebase with tree-sitter, builds an **LSP-oriented symbol graph whose every edge traces back to an exact source span**, and serves **hybrid retrieval** (BM25 + dense embeddings + regex/trigram + LLM re-ranking) to LLM code agents — exposed both as composable agent skills and over the **Model Context Protocol**. Incremental graph patching keeps the graph current without a full re-index.

Languages: **Python · Go · Rust · C/C++ · JavaScript · TypeScript**.

## Quickstart

```bash
pip install -e .
```

Build a query-only index for a repo, then serve it to any MCP-capable agent:

```python
from codeminer.compiler import IndexCompiler, IndexCompilerConfig
from codeminer.compiler.index_builders import IndexBuilderRegistry, register_default_builders

registry = IndexBuilderRegistry()
register_default_builders(registry, languages=["python"])
IndexCompiler(
    registry,
    IndexCompilerConfig(index_types=["bm25", "vector", "symbol_graph", "zoekt"]),
).compile_repo("/path/to/repo")  # writes <repo>/.codeminer_cache/repo_manifest.json
```

```bash
codeminer-mcp /path/to/repo/.codeminer_cache/repo_manifest.json   # stdio MCP server
codeminer-web                                                      # DeepWiki backend API
```

For the browser UI, start the Next.js frontend separately:

```bash
cd web && npm run dev
```

> **Optional — full code intelligence.** `make scip` installs the SCIP/LSP toolchain
> (rust-analyzer, scip-clang, scip-typescript, scip-python) for cross-file call graphs.

## Why CodeMiner

| | |
|---|---|
| **Edges you can trace** | The symbol graph is compiler-precise: every dependency edge resolves to an exact source location, so impact analysis and graph navigation point at real code — not fuzzy guesses. |
| **Built for LLM agents** | Retrieval ships as gated, composable [agent skills](docs/agent_skills.md) and as an [MCP server](docs/mcp.md) — drop CodeMiner into an agent without bespoke glue. |
| **Hybrid by default** | BM25, dense vectors, regex/trigram, and LLM re-ranking combine so name lookups, conceptual queries, and structural patterns all land. |
| **Stays fresh** | [Incremental graph patching](docs/incremental_graph/index.md) updates the graph in place from a diff instead of re-indexing the world. |

## Features

| Area | What it does | Docs |
|------|--------------|------|
| Web demo | DeepWiki-style wiki + Ask over indexed repos, with an interactive code-dependency graph | [Web Demo](docs/web_demo.md) |
| MCP server | Semantic / BM25 / regex / Zoekt search + dependency subgraphs over MCP | [MCP Server](docs/mcp.md) |
| Agent skills | Retrieval / rerank / trace skills with `allow_skills` gating and index-aware guards | [Agent Skills](docs/agent_skills.md) |
| Symbol graph | LSP-aligned line-range and symbol queries with typed results | [Graph Range Query](docs/graph_query.md) |
| Indexing | SCIP / language-server indexing with a tiered cache | [SCIP Index](docs/scip_index.md) |
| Datasets | SWE-bench ground-truth extraction + query synthesis pipeline | [Synthesis Pipeline](docs/synthesis_pipeline.md) |

## Documentation

Full docs are built with [mkdocs-material](https://squidfunk.github.io/mkdocs-material/):

```bash
pip install mkdocs-material
mkdocs serve   # http://localhost:8000
```

Start at [`docs/index.md`](docs/index.md).

## Development

```bash
make dev    # pip install -e ".[dev,test]"
make test   # pytest
```

Pre-commit hooks (black, isort, flake8) are configured — run `pre-commit install` after
cloning. The test suite is split into tiered pytest markers run as separate CI jobs; see
[CI/CD](docs/ci_cd.md) for the marker tiers and how to run each locally.

## License

CodeMiner is licensed under the [Apache License, Version 2.0](LICENSE).
Contributions previously made under the MIT License are retained under the terms of
Section 4 of Apache 2.0; see [NOTICE](NOTICE) for full attribution.
