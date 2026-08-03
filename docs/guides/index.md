---
title: Guides
hide:
  - toc
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Guides

<div class="codenib-section-lede" markdown>

Connect compiled repository views to coding agents, then choose the smallest
retrieval and planning surface that answers the task.

</div>

<div class="codenib-section-grid" markdown>

-   <span class="codenib-card-eyebrow">Serve context</span>

    **MCP Server**

    Build a reusable manifest and expose the full tool set, with results gated
    by fresh, available repository views.

    [Connect an MCP client →](../mcp.md)

-   <span class="codenib-card-eyebrow">Compose retrieval</span>

    **Agent skills**

    Add bounded lexical, semantic, graph-navigation, and reranking skills to an
    agent runtime.

    [Browse the skill surface →](../agent_skills.md)

-   <span class="codenib-card-eyebrow">Embed CodeNib</span>

    **Agent integrations**

    Use the LocAgent and OrcaLoca adapters while preserving CodeNib's manifest
    and source-location contracts.

    [Choose an integration →](../agent_integrations.md)

-   <span class="codenib-card-eyebrow">Execute safely</span>

    **Isolated agent execution**

    Run issue reproduction, edits, and tests through a provider-neutral,
    resource-bounded sandbox instead of the CodeNib service host.

    [Design an isolated worker →](../sandbox.md)

-   <span class="codenib-card-eyebrow">Plan a query</span>

    **RAG ops and planner**

    Understand retrieval operators, query-aware path selection, fusion,
    expansion, and reranking boundaries.

    [Inspect the retrieval policy →](../rag_ops.md)

</div>

## Recommended path

Use the [MCP Server](../mcp.md) for the standard agent-facing workflow. Reach
for [Agent Skills](../agent_skills.md) when you need to compose a custom runtime,
use [Isolated Agent Execution](../sandbox.md) before running repository code,
and use the [RAG Ops And Planner](../rag_ops.md) reference when changing query
policy rather than wiring.
