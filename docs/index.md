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

## User Guide

| Goal | Read |
|------|------|
| Turn a local repository into a Wiki | [Quickstart](quickstart.md) |
| Build an index and expose it to an agent | [MCP Server](mcp.md) |
| Configure the full browser application | [Web UI](web_demo.md) |
| Run without cloud-hosted LLM APIs | [Running Locally](running-locally.md) |
| Understand language support and gaps | [Language Capabilities](language_capabilities.md) |
| Add or promote a language backend | [Contributing a Language](contributing-a-language.md) |

Once an index exists, use [Agent Skills](agent_skills.md) for optional
retrieval and reranking workflows, or [RAG Ops And Planner](rag_ops.md) for the
query planner and graph-aware retrieval boundaries.

## Concepts

| Surface | Description |
|---------|-------------|
| [Index and MCP](mcp.md) | Build manifests with `IndexCompiler`; serve BM25, semantic, regex, Zoekt, and dependency-subgraph tools over MCP |
| [Web UI](web_demo.md) | Source-linked Wiki and optional Ask flow over indexed repositories |
| [Graph Range Query](graph_query.md) | LSP-aligned line-range and symbol queries with typed, source-linked results |
| [Incremental Graph](incremental_graph/index.md) | Update a graph in place after a diff where the language backend supports it |
| [SCIP Index](scip_index.md) | SCIP and language-server graph indexing details, cache levels, and backend behavior |
| [Core C++ Backend](core_cpp.md) | libigraph-based decoder acceleration and parity boundaries |

## Development

- [Contributing a Language](contributing-a-language.md) — add a language
  through the shared registry and verify each supported surface.
- [CI/CD](ci_cd.md) — run local and remote test tiers, including serial,
  graph-consumer, core, and slow jobs.
- [Releasing](releasing.md) — prepare and verify a CodeNib release.
- [Naming](branding.md) — use the project and package names consistently.

## Evaluation

- [Collecting SWE-bench Instances](collect_swebench.md) — sample
  representative instances across languages.
- [Synthesis Pipeline](synthesis_pipeline.md) — synthesize traversal,
  behavior, and multi-step queries for the CodeNib Synthesis benchmark.
- [GT Locator](gt_locator.md) — evaluate source-location retrieval against
  ground-truth patches.
- [Evaluation Artifact Bundles](evaluation_artifacts.md) — package
  reproducible evaluation outputs.

## Quick Start

```bash
pip install codenib
codenib wiki /path/to/repository
```

The default Wiki is deterministic, uses BM25, and needs no API key. CodeNib
detects repository languages, writes a reusable manifest under
`~/.codenib/repositories`, and opens the browser UI. This default `fast` profile
leaves the target checkout unchanged. Read the
[Quickstart](quickstart.md) for profiles and troubleshooting.

```bash
pip install "codenib[mcp]"
codenib mcp /path/to/repository
```

## Serving Docs Locally

```bash
pip install mkdocs-material
mkdocs serve
```

Then open [http://localhost:8000](http://localhost:8000).
