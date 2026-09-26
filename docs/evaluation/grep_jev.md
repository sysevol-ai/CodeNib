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

## Does the product preview reproduce these candidates?

An [offline product audit](../assets/grep_jev_product_audit.json) replays all
100 saved search plans through the actual product source reader, chunker,
ripgrep search and candidate limits. Each input is rebuilt from the experiment's
pinned Git blobs in a temporary directory; the original checkouts are unchanged.
Network calls are disabled. Labels are applied only after candidate selection.

All **100 cases complete**, and **94 have exactly the same ordered candidate
text and corrected source spans**. Six have different pools:

| Case | Research candidates | Product candidates |
| --- | ---: | ---: |
| jq #2235 | 26 | 99 |
| jq #2658 | 36 | 35 |
| Nushell #12950 | 58 | 57 |
| Redis #10068 | 22 | 22 |
| Redis #13338 | 16 | 16 |
| Valkey #1842 | 68 | 66 |

In jq #2235, text-mode ripgrep executes a saved regex containing `\x00` that
the research runner had skipped after a binary-mode error. Equal counts in
the Redis cases still contain different candidates. The frozen-plan product
grep ordering retains **58.62% Recall@5** across all 100 cases, but three pools
contain new text with no frozen Jev scores. The aggregate reranked result is
therefore deliberately left unset. **71.40% remains a research result.**

The planner request also identifies the repository by local directory name,
where the research runner used `owner/repo`. Replaying saved plans cannot
measure the effect on fresh planning. A complete product quality result needs
new planning and Jev calls, including failures, followed by an agent evaluation
before making token-saving claims.

A [fresh product attempt on 2026-09-26](../assets/grep_jev_product_live.json)
completed 19 cases before a Jev scoring call failed on Caddy #5870. That call
did not provide usable cost accounting, so the shared budget stopped the
remaining 80 cases. The recorded successful calls cost $0.225604746; the
failed call's cost is unknown. This is **not a complete quality result** and
does not change the 71.40% research claim. The report retains all 100 case
statuses and leaves aggregate recall unset. There were no automatic retries
or substitutions for the failed case.

`scripts/evaluate_grep_jev_product.py` runs fresh planning and Jev against the
same immutable source snapshots, without graph construction or embeddings.
It requires `--allow-billed-calls` and `--max-cost-usd`; these stop subsequent
calls based on reported usage and do not replace a provider billing cap.
Unknown cost stops the run. Operator-local traces retain candidates and usage;
the shared report omits queries and source. The current runner also retains
the failed scoring exception type and HTTP status when available, without
provider response bodies, headers or exception messages.

The source checkout includes `scripts/audit_grep_jev_product.py`. Supply the
frozen prepared corpus, Jev run, base-aligned labels and pinned repository
checkouts; its local output includes full candidate traces. The shared JSON
contains input/runtime/script hashes and all per-case counts and metrics,
without query text, source bodies or local filesystem paths.
