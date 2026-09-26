<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Model-planned grep → Jev: measured retrieval quality

**71.40% code-block Recall@5**, compared with **58.62%** for the same grep
candidates without reranking: a **12.78 percentage-point improvement** on
100 CodeNib Base issues. This is the research route reported in
[Let a Model Plan grep, Then Let Jev Rank the Code](https://codenib.ai/blogs/jev-model-grep-reranking/).
It is not the default retrieval route of the released `codegraph init` command.

## What was measured

The complete test split contains 100 issues across 25 repositories and five
language groups: C/C++, Go, Python, Rust, and TypeScript/JavaScript. It has
151 target code blocks tied to the issue's base commit. The dataset is
[CodeNib Base](https://huggingface.co/datasets/fishmingyu/codenib-base-dataset),
revision `4eb84e2e8918474969ce68c5b06facf14d6be604`.

Recall@5 is the fraction of target blocks retrieved in the first five results,
averaged across issues. It measures source localization, not whether an agent
fixed an issue or answered every question correctly.

| Route | Macro code-block Recall@5 |
| --- | ---: |
| Model-planned grep, original ordering | 58.62% |
| Model-planned grep → Jev | **71.40%** |
| BM25 → Jev | 59.35% |
| Dense (CodeRankEmbed) → Qwen3-Reranker-4B | 63.40% |
| Hybrid BM25 + dense → Qwen3-Reranker-4B | 65.03% |
| Hybrid BM25 + dense → Jev | 67.92% |

For grep → Jev against the unchanged grep ordering, the exploratory paired
95% bootstrap interval is **+6.44 to +19.67 percentage points**: 20 wins,
78 ties, and two regressions. These are results on this fixed split, not a
guarantee for an arbitrary repository.

## How the route works

An OpenRouter planning model proposes regex/glob searches. Local `rg` executes
them; tree-sitter maps matches to code blocks, deduplicates them, and retains
at most 100 candidates. Jev scores the visible candidate code through the
OpenRouter Decisions API, in batches of ten. Candidates are limited to
3,000 characters. The original range calculation included synthetic chunk
headers when counting visible lines. A subsequent
[offline range audit](../assets/grep_jev_range_audit.json) removed those header lines:
173 of 1,747 candidate ranges shortened, and the frozen rankings' Recall@5
remained 58.62% and 71.40%. That audit does not rerun candidate generation.
The [product preview](../guides/grep-jev.md) uses corrected ranges and has not
yet been evaluated end to end on this complete split.

The route does not need embeddings or a local GPU. It **does** use remote
models: query text and selected source snippets leave the local machine.
Both planning and Jev incur provider charges. The experiment's recorded
planning + Jev usage totaled $1.049122 for 100 issues; this is historical
reported API usage, not current pricing, a spending guarantee, or an account
of failed requests for which usage was unavailable.

## Reproduce and interpret

The [public experiment report](https://codenib.ai/blogs/jev-model-grep-reranking/)
describes candidate generation, pinned models, matched controls, timing,
hardware, and limitations. The source checkout includes
`scripts/benchmark_model_grep.py`, `scripts/benchmark_jev_base.py`, and
`scripts/benchmark_jev_dense.py` for research reproduction. The
[Jev guide](../jev.md) explains the existing reranker API.

This result does **not** establish a Claude Code token reduction, end-to-end
agent success rate, or DGX Spark latency. The timed comparisons are warm sums
of separately measured stages; setup, model loading, indexing, and recovery
waits are not production request latency. See the separate
[DGX Spark guide](../guides/reference-deployments/dgx-spark.md) for a local
deployment configuration.

For today's model-free agent setup, use [CodeGraph](../codegraph.md).
For the source-checkout OpenRouter route, use [grep and Jev](../guides/grep-jev.md).
For reproduced method contracts and scorer validation, use
[agent integrations](../agent_integrations.md) and the
[evaluation matrix](index.md).
