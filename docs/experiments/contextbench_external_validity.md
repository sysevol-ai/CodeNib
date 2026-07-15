<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# ContextBench external-validity study

This study tests whether the context-delivery result from the controlled
CodeMiner synthesis workload transfers to issue-driven tasks with human context
annotations. It is a localization and context-use study. It does **not** measure
patch correctness or compare complete coding agents.

## Frozen population

The source is the 50-row verified test file from
`Contextbench/ContextBench`, pinned to dataset revision
`c2855792b006af41c67202d33883fb9d46362853`. The parquet SHA-256 is
`4b560777bc8ba4061c7afa5f98ca2b5c793d0a32f085f25c5c817db0708b3629`;
the upstream evaluator is pinned to commit
`1436c28a8eb95496da4ea69ad458b9f8a8eb7d61`.

Selection is deterministic and outcome independent: retain every row whose
normalized owner/repository does not occur in the 25-repository synthesis
development corpus. The frozen result contains 41 issues from 18 repositories
and seven languages:

| language | issues |
|---|---:|
| Python | 19 |
| JavaScript | 6 |
| TypeScript | 5 |
| Java | 4 |
| Go | 3 |
| Rust | 3 |
| C | 1 |

The rows come from ContextBench Multi (14), Poly (12), Verified (10), and Pro
(5). They contain 337 normalized gold spans. Two exact duplicate annotations
are removed before scoring; no outcome-dependent row or span filtering is
allowed.

An initial audit incorrectly counted 43 issues from 22 repositories because
some Multi-SWE-bench rows identify repositories only as `core` or `jq`. The
frozen protocol recovers `vuejs/core` and `jqlang/jq` from
`original_inst_id`, then excludes both as development overlap. It also
preserves repository dotfiles such as `.size-limit.js`; applying Python's
`lstrip("./")` would silently change that path.

## Frozen analysis

The immutable protocol is
`/mnt/data/codeminer/results/contextbench_external_v1/protocol/manifest.json`.
Its file SHA-256 is
`2367f52f526106564f0a81c162d5858ea76de39ff46ffe362c29094b04accbcd`,
and its canonical payload SHA-256 is
`e19f38d7b2add9c31f8a929ea926feca319635d0f3817024e4d6c08219deca0a`.
The analysis plan is frozen at
`/mnt/data/codeminer/results/contextbench_external_v1/protocol/analysis_plan.json`
with SHA-256
`678420e38f31c42a44d6723ddf6e04e0041ec7a0677fa91184c7cf19b5b3f380`.

The two co-primary retrieval estimands are macro task-level File Coverage@10
and Line Coverage@10. Secondary estimands are macro gold-block Recall@10 and
micro context-weighted file and line coverage/precision. We report all frozen
estimands regardless of direction. Uncertainty uses a 10,000-sample
owner/repository-clustered percentile bootstrap with seed 20260713. Language
and benchmark-source breakdowns are descriptive because several strata are
small.

The official all-gold annotation is the primary analysis. A frozen sensitivity
analysis repeats the same estimands after excluding only gold blocks whose file
is absent from the exact base commit, and reports the excluded file/block
ledger. This exposes upstream scratch-file annotations without removing them
from the primary denominator.

Clone, index, and query failures remain in the 41-task denominator and receive
zero coverage/recall. Precision is conditional on completed retrievals, and
completion is reported separately. One `iamkun/dayjs` infrastructure smoke was
observed before freezing the estimands. It remains in the final denominator;
no cutoff, metric, arm, or case was selected from its outcome.

## System under test

Each exact `(owner/repository, base_commit)` snapshot is materialized once as a
content-addressed L2 vector artifact using
`Qwen/Qwen3-Embedding-0.6B` at revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`. The index is exact inner-product
FAISS Flat. Retrieval uses the issue statement unchanged and returns the first
ten source-linked candidates after the production preload assembly path.

The confirmatory agent protocol fixes Qwen3.5-9B, one repetition, and three
paired arms over all 41 issues: `grep_only`, `preinj_eager`, and
`preinj_eager_compact` (123 cells). Its primary metric is the paired total-token
ratio versus grep-only. The quality guardrail is committed-answer gold-block
Recall@5 over explicit `Locations:` spans, with absolute quality and paired
deltas reported. Graph-resolved `Symbols:` are disabled so every arm is scored
only on submitted repository locations. This follow-up tests delivery behavior;
it does not turn localization annotations into patch correctness labels.

The historical synthesis benchmark resolved submitted symbols against its graph.
Because that scorer is not equivalent to the location-only external scorer, we
do not pool effect estimates across the two studies. ContextBench is an
independent issue-driven validity gate, not an extension of the synthesis
sample.

The token estimator is the ratio of summed treatment tokens to summed baseline
tokens. A failed or missing cell contributes zero Recall@5; token ratios over
successful pairs remain descriptive, but a policy is confirmatory only when all
123 cells complete and the repository-clustered 95% lower bound on its paired
Recall@5 change is at least -0.05.

## Reproduce

```bash
python scripts/agent_compile/freeze_contextbench_study.py \
  --output /mnt/data/codeminer/results/contextbench_external_v1/protocol/manifest.json

python scripts/agent_compile/prepare_contextbench_study.py \
  --protocol /mnt/data/codeminer/results/contextbench_external_v1/protocol/manifest.json \
  --source-root /mnt/data/codeminer/results/contextbench_external_v1/sources \
  --output-root /mnt/data/codeminer/results/contextbench_external_v1/indexes \
  --summary /mnt/data/codeminer/results/contextbench_external_v1/build_summary.json \
  --batch-size 16

python scripts/agent_compile/evaluate_contextbench_retrieval.py \
  --protocol /mnt/data/codeminer/results/contextbench_external_v1/protocol/manifest.json \
  --prebuilt-root /mnt/data/codeminer/results/contextbench_external_v1/indexes \
  --output /mnt/data/codeminer/results/contextbench_external_v1/retrieval_records.json

python scripts/agent_compile/summarize_contextbench_retrieval.py \
  --protocol /mnt/data/codeminer/results/contextbench_external_v1/protocol/manifest.json \
  --records /mnt/data/codeminer/results/contextbench_external_v1/retrieval_records.json \
  --output /mnt/data/codeminer/results/contextbench_external_v1/retrieval_summary.json

python scripts/agent_compile/summarize_contextbench_agents.py \
  --protocol /mnt/data/codeminer/results/contextbench_external_v1/protocol/manifest.json \
  --results-root /mnt/data/codeminer/results/contextbench_external_v1/agent_qwen35_9b \
  --output /mnt/data/codeminer/results/contextbench_external_v1/agent_summary.json
```

`build_summary.json` schema 2 distinguishes the fixed `planned` denominator,
the durable `recorded` ledger, and `pending` rows. This makes interrupted and
resumed batches auditable without mistaking partial progress for the protocol
size.
