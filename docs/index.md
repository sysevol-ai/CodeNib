<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeMiner

A code analysis agent with graph-enhancement for multi-language codebases.

## Overview

CodeMiner provides tools for structural code analysis, symbol-level change detection, and graph-based code intelligence. Language support varies by surface; see the [Language Capabilities](language_capabilities.md) matrix for chunking, graph, incremental, and core parity coverage.

### Key Components

| Component | Description |
|-----------|-------------|
| [Web Demo](web_demo.md) | DeepWiki-style wiki + Ask site with an interactive code-dependency graph over indexed repos |
| [MCP Server](mcp.md) | Serve semantic/BM25/regex/Zoekt search and dependency subgraphs to LLM agents over MCP |
| [Agent Skills](agent_skills.md) | Composable retrieval/rerank/trace skills with index-aware gating |
| [Language Capabilities](language_capabilities.md) | Registry-derived support matrix across chunking, graph, incremental, and core parity surfaces |
| [GT Locator](gt_locator.md) | Extract symbol-level ground truth from SWE-bench patches |
| [Incremental Graph](incremental_graph/index.md) | Update CodeGraph in-place without full re-indexing |
| [Graph Range Query](graph_query.md) | LSP-aligned line-range / symbol queries with typed results |
| [Regex Index](regex_index.md) | Fast regex search across CodeGraph nodes |
| [SCIP Index](scip_index.md) | Code intelligence via the SCIP protocol |
| [Graph Cache](graph_cache_usage.md) | Caching system for SCIP indexing |
| [Core C++ Backend](core_cpp.md) | libigraph-based decoder mirroring the Python pipeline |

### Guides

- [Collecting SWE-bench Instances](collect_swebench.md) — sample representative instances across languages
- [Synthesis Pipeline](synthesis_pipeline.md) — synthesize traversal/behavior/multiply queries and build the CodeMiner dataset
- [Uploading to HuggingFace](upload_dataset_to_huggingface.md) — build and publish the dataset
- [CI/CD](ci_cd.md) — pipeline setup and test tiers
- [Diagnose Query Leak](diagnose_query_leak.md) — detect lexical/semantic leakage in synthesized queries

## Quick Start

```bash
pip install -e ".[dev]"
```

## Serving Docs Locally

```bash
pip install mkdocs-material
mkdocs serve
```

Then open [http://localhost:8000](http://localhost:8000).
