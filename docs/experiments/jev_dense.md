<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Dense and hybrid controls for the Jev comparison

The matched 100-issue experiment compares complete embedding retrieval plus
reranking paths against BM25 and model-planned grep. **Grep → Jev reached
71.40% code-block Recall@5, versus 63.40% for dense → Qwen 4B and 65.03% for
BM25+dense → Qwen 4B.** The exploratory repository-bootstrap interval excludes
zero for the gain over pure dense, but not over hybrid. These results
support a useful alternative on this sample, not a general replacement claim.

This fills the control missing from the [BM25/grep experiment](jev_candidates.md).
The [15-question exploration](jev_retrieval.md) used a different corpus and
file-group metric and cannot answer this full-path comparison.

## Accuracy before and after reranking

All rows use the same 100 CodeNib Base issues, 25 repositories, 151 target
blocks aligned to `base_commit`, and at most 100 candidates. Dense, hybrid and
BM25 each return 100; grep returns 17.47 on average. Coverage measures hits
anywhere in the candidate pool. Recall@5 measures the final non-overlapping
visible spans, with the existing first-wins deduplication rule.

| Retrieval route | Candidate coverage | No reranker R@5 | Qwen 4B R@5 | Jev R@5 |
| --- | ---: | ---: | ---: | ---: |
| BM25 | 63.78% | 28.78% | 58.30% | 59.35% |
| CodeRankEmbed + FAISS | 71.98% | 39.75% | 63.40% | 63.05% |
| BM25 + dense, RRF | 75.28% | 32.02% | 65.03% | 67.92% |
| Model-planned grep | 75.83% | 58.62% | 68.07% | 71.40% |

Grep's candidate-coverage advantage is 12.05 percentage points over BM25,
but only 0.55 points over hybrid. The broader controls therefore materially
change the comparison. High candidate coverage also does not guarantee good
ordering: hybrid's unreranked Recall@5 is lower than dense's.

## Complete paths and uncertainty

Differences are **grep → Jev minus the named complete path**, using the same
queries and aligned labels. Intervals resample 25 repository clusters with
replacement, 5,000 draws, seed 20260923. They are exploratory 95% intervals
without multiple-comparison adjustment, not equivalence tests.

| Comparison path | R@5 difference | Paired 95% interval | Wins / ties / losses |
| --- | ---: | ---: | ---: |
| Dense → Qwen 4B | +8.00 pp | [+0.91, +15.26] | 19 / 73 / 8 |
| Hybrid → Qwen 4B | +6.37 pp | [−1.29, +13.87] | 19 / 72 / 9 |

Changing only the reranker is a different question. Jev minus Qwen 4B is
−0.35 pp [−5.47, +5.03] on dense candidates and +2.88 pp [−1.87, +8.08] on
hybrid candidates. Neither interval establishes a quality advantage or
quality equivalence. The complete-path gains must not be attributed solely
to Jev. Merely replacing the traditional path with BM25 → Jev also gives a
lower Recall@5 point estimate than either embedding → Qwen 4B baseline.

On the same grep candidates, Jev still improves unreranked Recall@5 by
12.78 pp [+6.44, +19.67], with 20 wins, 78 ties and two losses. This baseline
is recomputed with the same aligned coordinates as every other row.

## Latency, cost and availability

These are warm **sums of separately measured stages per issue**, followed by
the percentile calculation. Grep includes successful planning and local
search. They exclude model loading, index construction, the original grep
planning run's 429 failures, and recovery waits. They are not observed
production end-to-end requests. BM25 search was remeasured at top-100 during
the new preparation; its stage sums differ slightly from the earlier report.

| Retrieval | Retrieval p50 | + Qwen 4B p50 / p95 | + Jev p50 / p95 | Recorded API cost for retrieval + Jev / 100 issues |
| --- | ---: | ---: | ---: | ---: |
| BM25 | 136 ms | 5.364 / 11.514 s | 3.207 / 4.575 s | $0.288725 |
| Dense | 18 ms | 4.571 / 12.050 s | 3.175 / 4.034 s | $0.238814 |
| Hybrid | 160 ms | 5.146 / 12.629 s | 3.412 / 4.727 s | $0.257580 |
| Model-planned grep | 4,081 ms | 4.658 / 8.132 s | 4.592 / 6.787 s | $1.049122 |

Grep → Jev had essentially the same median stage sum as dense → Qwen 4B,
and a 10.8% lower point estimate than hybrid → Qwen 4B. Removing embeddings
was not itself the latency saving: the dense lookup, including query encoding,
had an 18 ms median. Fewer candidates and a different reranker changed most
of the work. Keeping embeddings and using hybrid → Jev reached 67.92% R@5
at a 3.412 s median, another quality/latency tradeoff.

The two new Jev sweeps made 2,000 requests and reported **$0.496394** in total.
One dense-route request timed out at 30 seconds; hybrid had no failed calls.
Fallback ordering remains in the accuracy and latency results. That timeout
also contributes to dense → Jev's 32.572 s maximum. Both new Qwen sweeps had
zero errors. All 1,999 successful new Jev responses resolved to
`typesafe/jev-1.13-20260917`. Missing failed-call usage remains unknown. Local
GPU costs are not priced; grep planning alone cost $1.003761 across 100 issues
and applies to its Qwen path too.

## Matched method

- Dataset: `fishmingyu/codenib-base-dataset`, revision
  `4eb84e2e8918474969ce68c5b06facf14d6be604`, complete test split. The new
  preparation verifies Git trees, selected-blob hashes, chunk counts, and
  candidate content against the frozen BM25 experiment. No targets, patches,
  hints or scores affect candidate selection.
- Both BM25 and dense index full production tree-sitter chunks, depth 2 and
  at most 100 lines. Every reranker sees at most 3,000 characters per candidate;
  evaluation uses only the corresponding visible line range.
- Dense uses the production CodeRankEmbed wrapper, revision
  `3c4b60807d71f79b43f3c4363786d9493691f8b1`, its registered query instruction,
  raw chunk content as in `CodeVectorStore.add_code_chunks`, float32 on the
  H100 PCIe, normalized vectors, and exact FAISS `IndexFlatIP` with one CPU
  thread. The native context is 8,192 tokens. All 100 queries fit (maximum
  6,246). Twenty occurrences of five unusually long SymPy chunks were
  truncated during embedding; none entered either new top-100 pool and none
  overlapped a target in its visible range. These chunks were not removed.
- Hybrid takes top-100 from each branch and uses the production RRF merger:
  equal weights, rank constant 60, a final cap of 100, and BM25 first for stable
  ties. Fusion uses the production node identity rule. These settings were
  fixed before examining new quality results, not tuned on the target labels.
- Qwen 4B uses revision `22e683669bc0f0bd69640a1354a6d0aebcfeede5`, BF16,
  batch size 8, final-position logits and no KV cache, as in the earlier
  experiment. All 20,000 new pairs fit its 12,288-token input limit; the maximum
  was 10,742. Jev uses sequential batches of ten four-level score questions.
- There are 400 new observations: two new routes × two rerankers × 100 issues,
  one run per combination. The matched four-route analysis also reuses 400
  BM25/grep observations, for 800 reranked rows and 400 unreranked controls.
  The original Qwen 0.6B arms remain a separate supplementary comparison.
- Local GPU sweeps run sequentially. Hosted Jev scoring overlaps Qwen scoring,
  as in the earlier experiment. Model loading and document/index construction
  remain outside online timing.

Across 100 source snapshots, 617,486 chunk occurrences reused embeddings of
identical text within each repository. Encoding 219,383 distinct inputs across
those repository caches took 753.608 seconds; FAISS builds took 3.952 seconds.
Cached model loading plus warm-up took 6.093 seconds. These are aggregate build
measurements with text reuse, not per-query cold-start latency. Source
materialization and token auditing add preparation work outside those totals.

## Artifacts and reproduction

[Results JSON](jev_dense_results.json) contains all aligned aggregate metrics,
paired comparisons, configurations and raw artifact hashes.
[Per-case CSV](jev_dense_cases.csv) contains 1,200 rows: each issue, route and
ordering, including the unreranked baselines. The raw experiment root is
`/mnt/data/zhongming/codenib-jev-dense-20260923-main-v1`; the four scoring roots
share the `codenib-jev-dense-20260923-` prefix. It also retains the fixed protocol,
exact executed producer, environment record, and token/truncation audits.
The committed producer has the same Python AST as the executed copy.

A portable raw-data bundle is retained on the experiment machine at
`/mnt/data/zhongming/codenib-jev-dense-controls-20260923.tar.gz` (14,779,137 bytes).
It includes the four prepared pools, all eight scoring runs, label audit,
analysis outputs and scripts. All 545 files were checked against the bundled
`SHA256SUMS`. Archive SHA-256:
`007986cc074ce3260ccd3aa6c0610df7263ca7376a4ddf313c36e077b1a64741`.

First prepare the prior BM25/grep artifacts following
[the candidate experiment](jev_candidates.md#reproduction-and-artifacts). Pinned model
snapshots must be cached locally. Use fresh output directories. Loading the
OpenRouter credential and passing `--live` enables billed requests.

```bash
export PRIOR_EVAL=/path/to/jev-candidates
export DENSE_EVAL=/path/to/new/dense-controls
export BASE_REPOS=/path/to/base-repositories
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

python scripts/benchmark_jev_dense.py \
  --bm25-prepared "$PRIOR_EVAL/bm25" --prebuilt-root "$BASE_REPOS" \
  --output "$DENSE_EVAL/prepared"
for route in dense hybrid; do
  python scripts/benchmark_jev_base.py run \
    --prepared "$DENSE_EVAL/prepared/$route" \
    --output "$DENSE_EVAL/$route-4B" --backend qwen \
    --model Qwen/Qwen3-Reranker-4B --repeats 1 --batch-size 8
  python scripts/benchmark_jev_base.py run \
    --prepared "$DENSE_EVAL/prepared/$route" \
    --output "$DENSE_EVAL/$route-jev" --backend jev \
    --model typesafe/jev-1.13 --repeats 1 --live --max-cost-usd 0.75
done
python scripts/analyze_jev_dense.py \
  --bm25-prepared "$PRIOR_EVAL/bm25" --grep-prepared "$PRIOR_EVAL/grep" \
  --dense-prepared "$DENSE_EVAL/prepared/dense" \
  --hybrid-prepared "$DENSE_EVAL/prepared/hybrid" \
  --label-report "$PRIOR_EVAL/results.json" \
  --runs "$PRIOR_EVAL/bm25-4B" "$PRIOR_EVAL/bm25-jev" \
    "$PRIOR_EVAL/grep-4B" "$PRIOR_EVAL/grep-jev" \
    "$DENSE_EVAL/dense-4B" "$DENSE_EVAL/dense-jev" \
    "$DENSE_EVAL/hybrid-4B" "$DENSE_EVAL/hybrid-jev" \
  --output "$DENSE_EVAL/results.json" --case-csv "$DENSE_EVAL/cases.csv"
```

This measures localization on public historical issues, not fixes or complete
answers. Label completeness, possible training exposure, other embedding
models, CPU/remote embedding deployments, fusion tuning, and production
cold-start/throughput behavior remain outside the experiment. The sample
cannot establish general superiority or non-inferiority, and no production
retrieval default changes on the strength of these results.
