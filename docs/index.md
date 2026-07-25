<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib

CodeNib compiles repositories into reusable, source-linked context for coding
agents and developers. Start with a local Wiki, then reuse the same manifest
through MCP, Python APIs, and evaluation harnesses.

Language support varies by surface. Start with the generated
[Language Capabilities](language_capabilities.md) matrix when you need to know
which languages support chunking, graph indexing, incremental patching, or C++
core decoder parity.

## Start Here

| Goal | Read |
|------|------|
| Turn a local repository into a Wiki | [Quickstart](quickstart.md) |
| Build an index and expose it to an agent | [MCP Server](mcp.md) |
| Configure the full browser application | [Web UI](web_demo.md) |
| Run without cloud-hosted LLM APIs | [Running Locally](running-locally.md) |
| Understand language support and gaps | [Language Capabilities](language_capabilities.md) |
| Add or promote a language backend | [Contributing a Language](contributing-a-language.md) |

## Core Surfaces

| Surface | Description |
|---------|-------------|
| [Index and MCP](mcp.md) | Build manifests with `IndexCompiler`; serve BM25, semantic, regex, Zoekt, and dependency-subgraph tools over MCP |
| [Web UI](web_demo.md) | Source-linked Wiki and optional Ask flow over indexed repositories |
| [Graph Range Query](graph_query.md) | LSP-aligned line-range and symbol queries with typed, source-linked results |
| [Incremental Graph](incremental_graph/index.md) | Update a graph in place after a diff where the language backend supports it |
| [SCIP Index](scip_index.md) | SCIP and language-server graph indexing details, cache levels, and backend behavior |
| [Core C++ Backend](core_cpp.md) | libigraph-based decoder acceleration and parity boundaries |

## Developer Guides

- [CI/CD](ci_cd.md) — local and remote test tiers, including serial, graph-consumer, core, and slow jobs
- [Agent Skills](agent_skills.md) — optional retrieval/rerank/trace skills built on top of the index substrate
- [RAG Ops And Planner](rag_ops.md) — retrieval operator boundaries, query-aware planner behavior, and graph-plan limits
- [Collecting SWE-bench Instances](collect_swebench.md) — sample representative instances across languages
- [Synthesis Pipeline](synthesis_pipeline.md) — synthesize traversal/behavior/multiply queries for the CodeNib Synthesis benchmark
- [Uploading to HuggingFace](upload_dataset_to_huggingface.md) — build and publish the dataset
- [Diagnose Query Leak](diagnose_query_leak.md) — detect lexical/semantic leakage in synthesized queries

## Research And Roadmaps

These documents guide development decisions, but they are not required for the
normal index -> retrieve/query -> serve path.

- [SCIP Multi-Language Roadmap](scip_multilanguage_roadmap.md) — cold-start backend promotion, C++ acceleration gates, and long-running language work
- [Agent Runner Architecture Goal](agent_runner_architecture_goal.md) — milestones and guardrails for extracting runner core from spike experiments
- [Agent Compile Design](agent_compile_design.md) — historical design notes for scenario-gated agent skill selection
- [Agent Compile Sweep](experiments/agent_compile.md) — experiment log and ablations for agent harness behavior

## Quick Start

```bash
pip install codenib
codenib wiki /path/to/repository
```

The default Wiki is deterministic, uses BM25, and needs no API key. CodeNib
detects repository languages, writes a reusable manifest under
`.codenib_cache`, and opens the browser UI. Read the
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
