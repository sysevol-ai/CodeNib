<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# News

Product releases, integration updates, and notable additions to CodeNib. For
the complete version history, see
[GitHub Releases](https://github.com/sysevol-ai/CodeNib/releases) and the
[changelog](https://github.com/sysevol-ai/CodeNib/blob/main/CHANGELOG.md).

## 2026-08-21 — Repository source authority

CodeNib 0.2.2 admits absolute symlinks through authenticated indexing only
when they remain inside the same pinned checkout. Exact root-relative source
exclusions now persist across indexing, CodeGraph, MCP, Wiki, and Web runtimes.

[Read the 0.2.2 release notes](releases/0.2.2.md)

## 2026-08-13 — Agent-ready CodeGraph onboarding

One command prepares a repository graph and connects it to Codex and Claude
Code. The workflow includes idempotent status, safe uninstall, and
installed-wheel MCP graph verification.

[Open the CodeGraph guide](codegraph.md)

## 2026-08-08 — RepoNavigator Jump adapter

The published single-tool
[RepoNavigator](https://arxiv.org/abs/2512.20957v6) contract resolves symbol
definitions through a persisted SCIP occurrence or injected LSP signal, with
graph-only resolution disclosed as a degraded fallback.

[Review the support boundary](agent_integrations.md#reponavigator)

## 2026-08-05 — Static Wiki and reusable context artifacts

CodeNib 0.2.0 introduced a build-once path for static Wikis and reusable
context artifacts, serving them through GitHub Pages or the official MCP
package alongside hybrid retrieval and managed SCIP/LSP providers.

[Read the 0.2.0 release notes](releases/0.2.0.md)

## 2026-08-05 — SweRank retrieval and reranking

The SweRank recipe runs
[SweRank](https://github.com/SalesforceAIResearch/SweRank) retrieval and
reranking over a local checkout.

[Open the example](https://github.com/sysevol-ai/CodeNib/blob/main/examples/swerank_retrieve_rerank.py)

## 2026-08-04 — Native repository explorer

CodeNib's planner targets the
[SWE-Explore](https://github.com/Qiushao-E/SWE-Explore-Bench) source-region
protocol for native repository exploration.

[Review the validation](evaluation/swe_explore.md)

## 2026-08-03 — Native LocAgent policy

[LocAgent](https://github.com/gersteinlab/LocAgent) runs directly on CodeNib
views without LocAgent, LiteLLM, or LlamaIndex dependencies.

[Open the agent support matrix](agent_integrations.md)

## 2026-08-02 — OrcaLoca SearchAgent

[OrcaLoca](https://github.com/fishmingyu/OrcaLoca)'s six-tool search loop
reuses CodeNib's symbol graph.

[Open the agent support matrix](agent_integrations.md)
