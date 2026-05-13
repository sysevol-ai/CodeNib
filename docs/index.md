<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeMiner

A code analysis agent with graph-enhancement for multi-language codebases.

## Overview

CodeMiner provides tools for structural code analysis, symbol-level change detection, and graph-based code intelligence. It supports **Python, Go, Rust, C/C++, JavaScript, and TypeScript**.

### Key Components

| Component | Description |
|-----------|-------------|
| [GT Locator](gt_locator.md) | Extract symbol-level ground truth from SWE-bench patches |
| [Incremental Graph](incremental_graph.md) | Update CodeGraph in-place without full re-indexing |
| [Regex Index](regex_index.md) | Fast regex search across CodeGraph nodes |
| [SCIP Index](scip_index.md) | Code intelligence via the SCIP protocol |
| [Graph Cache](graph_cache_usage.md) | Caching system for SCIP indexing |

### Guides

- [Collecting SWE-bench Instances](collect_swebench.md) — sample representative instances across languages
- [Uploading to HuggingFace](upload_dataset_to_huggingface.md) — build and publish the dataset

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
