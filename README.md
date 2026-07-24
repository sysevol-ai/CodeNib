<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib

**Source-linked code intelligence for LLM tools** — multi-language indexing,
hybrid retrieval, dependency graphs, and a Model Context Protocol server.

[![CI](https://github.com/sysevol-ai/CodeMiner/actions/workflows/ci.yml/badge.svg)](https://github.com/sysevol-ai/CodeMiner/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-526CFE.svg)](docs/index.md)

CodeNib builds reusable indexes over a repository, then serves those indexes
to agents, web UIs, and evaluation harnesses. The core surfaces are:

- source chunking and repository manifests;
- BM25, dense-vector, regex/trigram, Zoekt, and rerank retrieval;
- SCIP/LSP-backed symbol graphs with source-linked nodes and edges;
- a stdio **Model Context Protocol** server for agent tools;
- a DeepWiki-style web UI for browsing indexed repositories.

> **Naming compatibility:** CodeNib is the product name. The Python package,
> existing `codeminer-*` commands, `CODEMINER_*` environment variables,
> `.codeminer*` state paths, repository URL, and published dataset identities
> remain stable during this migration.

Language coverage varies by surface. See the generated
[Language Capabilities](docs/language_capabilities.md) matrix for the current
chunking, graph, incremental, and C++ core parity status.

## Quickstart

```bash
pip install -e .
```

Build an index for a repository:

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

Serve that manifest to any MCP-capable agent:

```bash
codenib-mcp /path/to/repo/.codeminer_cache/repo_manifest.json   # stdio MCP server
```

The compatibility alias `codeminer-mcp` reaches the same implementation.

For the browser UI, start the managed backend and frontend:

```bash
make web-deps
make web-start                                                     # backend :8000, frontend :3000
```

For a no-cloud local GPU LLM setup, copy `qa_config.local.yaml.example` to the
ignored `qa_config.local.yaml` and follow [Running Locally](docs/running-locally.md).

> **Optional — full graph indexing.** `make scip` installs the active SCIP/LSP
> toolchain used by the graph backends. `make multilang-tools` installs the wider
> cold-start smoke toolchains tracked in the language matrix.

## Why CodeNib

| | |
|---|---|
| **Reusable index substrate** | Build once with `IndexCompiler`, then reuse the same manifest from MCP, the web UI, tests, and eval scripts. |
| **Retrieval before orchestration** | BM25, dense vectors, regex/trigram, Zoekt, and rerank paths are first-class. Agent skills are an integration surface, not the only way to use the system. |
| **Source-linked graph context** | SCIP/LSP graph backends preserve symbol names, locations, and dependency edges so graph navigation points back to real code spans. |
| **Operationally testable** | CI is split into unit, integration, serial integration, core, graph-consumer, and slow tiers so heavy graph/LLM work is explicit. |

## Features

| Area | What it does | Docs |
|------|--------------|------|
| Index compiler | Builds manifests and index artifacts for downstream tools | [MCP Server](docs/mcp.md) |
| Search and retrieval | BM25, semantic, regex, Zoekt, and rerank retrieval over indexed code | [MCP Server](docs/mcp.md) |
| Dependency graph | Symbol graph queries, source-linked edges, and incremental patching where supported | [Graph Range Query](docs/graph_query.md), [Incremental Graph](docs/incremental_graph/index.md) |
| Web demo | DeepWiki-style wiki + Ask over indexed repos, with an interactive code-dependency graph | [Web Demo](docs/web_demo.md) |
| MCP server | Semantic / BM25 / regex / Zoekt search + dependency subgraphs over MCP | [MCP Server](docs/mcp.md) |
| Agent and eval harness | Optional skills, traces, sweeps, and experiments built on top of the same indexes | [Agent Skills](docs/agent_skills.md), [Experiments](docs/experiments/agent_compile.md) |
| Datasets | SWE-bench ground-truth extraction + query synthesis pipeline | [Synthesis Pipeline](docs/synthesis_pipeline.md) |

## Project Boundaries

The main product path is index -> retrieve/query -> serve. Experimental agent
harnesses, graph-routing studies, and SCIP cold-start roadmaps live in the docs
because they guide development, but they are not prerequisites for using
CodeNib as an MCP server or web-backed code browser.

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

CodeNib is licensed under the [Apache License, Version 2.0](LICENSE).
Contributions previously made under the MIT License are retained under the terms of
Section 4 of Apache 2.0.
