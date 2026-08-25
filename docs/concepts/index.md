---
title: Concepts
hide:
  - toc
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Concepts

<div class="codenib-section-lede" markdown>

CodeNib compiles one repository into several source-linked views. These pages
explain what each view preserves, how the views align, and where their support
boundaries differ.

</div>

<div class="codenib-section-grid" markdown>

-   <span class="codenib-card-eyebrow">Static navigation</span>

    **SCIP index**

    Follow language-aware definitions and references through SCIP or LSP
    backends and understand cache-level behavior.

    [Understand SCIP indexing →](../scip_index.md)

-   <span class="codenib-card-eyebrow">Structural view</span>

    **CodeGraph**

    Learn the typed hierarchy model and query graph nodes by source-aligned
    symbol or line range.

    [Read the hierarchy model →](../codegraph_hierarchy_model.md)

-   <span class="codenib-card-eyebrow">Commit-aware updates</span>

    **Incremental graph**

    See how a diff updates supported graph views in place, when verification
    admits the patch, and when CodeNib rebuilds.

    [Explore incremental updates →](../incremental_graph/index.md)

-   <span class="codenib-card-eyebrow">Retrieval planning</span>

    **RAG ops and planner**

    Understand retrieval operators, query-aware path selection, fusion,
    expansion, reranking, and the boundary between policy and execution.

    [Inspect the retrieval model →](../rag_ops.md)

-   <span class="codenib-card-eyebrow">Native acceleration</span>

    **Core C++ backend**

    Understand decoder parity, libigraph-backed execution, fallback behavior,
    and the boundary between Python and native code.

    [Inspect the native core →](../core_cpp.md)

</div>

## Other index views

- [Regex Index](../regex_index.md) covers structural pattern lookup.
- [Graph Range Query](../graph_query.md) defines source-range and symbol query
  semantics.
- [Graph Cache](../graph_cache_usage.md) explains persisted graph reuse and
  invalidation.
- [Interactive Incremental Graph](../incremental_graph/interactive.md) provides
  a visual walkthrough of commit-scoped graph changes.
