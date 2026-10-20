---
title: Get Started
hide:
  - toc
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Get Started

<div class="codenib-section-lede" markdown>

Connect a repository to your coding agent first. Add a browsable Wiki, local
models, or the optional grep → Jev route when you need them.

</div>

<div class="codenib-section-grid" markdown>

-   <span class="codenib-card-eyebrow">01 · Coding agents</span>

    **Agent-ready CodeGraph**

    One command builds BM25 and a typed symbol graph, then registers MCP
    tools with Codex and Claude Code. No model or API key.

    [Connect your agent →](../codegraph.md)

-   <span class="codenib-card-eyebrow">02 · First Wiki</span>

    **Quickstart**

    Turn a local repository into a source-linked Wiki with reusable hybrid
    retrieval and no API key.

    [Launch your first repository →](../quickstart.md)

-   <span class="codenib-card-eyebrow">03 · Choosing tools</span>

    **Compare repository tools**

    See what grep, CodeNib, Serena, CodeGraph, and DeepWiki each let an agent
    ask, and how each resolves relationships.

    [Compare the options →](../comparison.md)

-   <span class="codenib-card-eyebrow">04 · Browser workflow</span>

    **Web UI**

    Configure the repository Wiki, source explorer, graph views, and optional
    Ask experience.

    [Configure the Web UI →](../web_demo.md)

-   <span class="codenib-card-eyebrow">05 · Local stack</span>

    **Run locally**

    Keep embeddings, reranking, and page generation on infrastructure you
    control.

    [Choose a local deployment →](../running-locally.md)

-   <span class="codenib-card-eyebrow">06 · Backend coverage</span>

    **Language capabilities**

    Check which languages support chunking, SCIP or LSP graphs, incremental
    updates, and native decoder acceleration.

    [Open the capability matrix →](../language_capabilities.md)

</div>

## Add language providers

CodeNib detects installed semantic capabilities automatically. Structural and
live-navigation providers are language-specific; use `codenib toolchain` to
inspect or install only the tools needed by one repository. The
[Quickstart](../quickstart.md#language-toolchains) shows the complete flow.

## Next steps

- [Context methods and support matrix](../concepts/methods.md) explains how
  the CodeGraph tools relate to the other retrieval routes.
- [Search with grep and Jev](../guides/grep-jev.md) adds a no-index route that
  uses your OpenRouter account.
- [GitHub Pages](../github_pages.md) publishes a static Wiki and a reusable
  context artifact.
