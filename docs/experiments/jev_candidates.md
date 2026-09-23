<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Candidate budgets and model-planned grep on CodeNib Base

This follow-up to the [top-50 comparison](jev_base.md) keeps the entire pinned
CodeNib Base test split: 100 issues, 25 repositories, five language groups,
dataset revision `4eb84e2e8918474969ce68c5b06facf14d6be604`, and source at each
instance's `base_commit`. The code base is main
`61a9ab2fd2cc8f656fa541d6891289f30531f2e7` plus the local Jev integration.

The [matched embedding controls](jev_dense.md) extend this comparison with dense
and BM25+dense retrieval on the same 100 issues. Use that report to compare
complete embedding-plus-reranker paths against the embedding-free routes; the
BM25/grep comparison on this page alone cannot establish that replacement.

The requested candidate budget is **100**. BM25 recall is also audited at
50, 200, 500, and 1000 without making model calls at those larger budgets.
The second retrieval route lets Sonnet 4.6 plan grep actions and passes the
resulting code chunks to the same dedicated rerankers and Jev. It uses no
embeddings. This is a one-shot planner experiment, not a multi-turn coding
agent evaluation.

**Model-planned grep improves final code-block recall with far fewer candidates,
at the cost of a planning call.** After aligning target symbols to the actual
base commit, grep → Jev reaches **71.40% Recall@5**, versus **59.35%** for
BM25 top-100 → Jev. Jev has the highest point estimate on the grep pool, but
its difference from Qwen 4B is not statistically resolved after the label audit.
All quality figures in the following results table use base-aligned ranges.
The [machine-readable results](jev_candidates_results.json) retain both frames
and every label mapping; the [100-instance CSV](jev_candidates_cases.csv)
includes separate `base_aligned/` columns.

## Reranking results

Each cell in the recall columns is macro code-block Recall@5. Each reranker
sees identical candidates within its retrieval route. BM25 supplies 100
chunks per issue; grep supplies at most 100, averaging 17.47. There are 600
new scoring observations: 100 issues × two retrieval routes × three rerankers,
with one repeat. Candidate preparation and model loading are excluded.

| Reranker | BM25 R@5 | Grep R@5 | BM25 rerank p50 | Grep rerank p50 | BM25 stage sum p50 | Grep stage sum p50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Qwen3-Reranker-0.6B | 48.40% | 62.48% | 1.666 s | 0.249 s | 1.819 s | 4.356 s |
| Qwen3-Reranker-4B | 58.30% | 68.07% | 5.101 s | 0.717 s | 5.253 s | 4.658 s |
| Jev 1.13 | 59.35% | **71.40%** | 3.010 s | 0.480 s | 3.171 s | 4.592 s |

The stage sums are **offline sums of separately measured components**,
including the planner for grep. They exclude the initial planning run's 429
failures and offline recovery scheduling, and should not be read as observed
production end-to-end latency. Final successful planning has p50 4.044 s and
p95 5.580 s; local grep/context extraction has p50 33 ms. The BM25 top-100
control search has p50 102 ms.

Grep improves Jev's Recall@5 by **12.05 percentage points**, with a paired
repository-bootstrap 95% interval of **[+5.16, +19.40]**. Corresponding route
improvements are +14.08 pp [+4.71, +24.29] for 0.6B and +9.77 pp
[+1.17, +18.07] for 4B. These gains survive the coordinate correction.

Within the grep pool, Jev minus 4B is +3.33 pp, interval **[−0.18, +7.59]**;
within BM25 it is +1.05 pp [−3.44, +5.52]. Neither establishes a Recall@5
advantage over 4B. Jev's advantage over 0.6B remains resolved in both pools:
+8.92 pp [+3.50, +15.19] on grep and +10.95 pp [+3.84, +19.18] on BM25.
The models still differ in latency, cost, and hosted availability.

| Reranker | BM25 rerank p95 / max | Grep rerank p95 / max |
| --- | ---: | ---: |
| Qwen 0.6B | 3.562 s / 28.150 s | 0.891 s / 2.714 s |
| Qwen 4B | 10.932 s / 79.154 s | 2.691 s / 7.555 s |
| Jev | 3.580 s / 4.477 s | 1.569 s / 2.620 s |

These are serving-configuration measurements, not intrinsic model speed
comparisons. GPU model sweeps ran sequentially; hosted scoring overlapped
some local GPU work. The measured savings from fewer candidates can offset
planning cost for the 4B route, while the BM25 → 0.6B route remains fastest.

Jev and 4B still make different ranking errors. For `gin-gonic__gin-3820`,
grep finds the missing candidates and Jev places `setWithProperType()` third;
4B's first five spans favor multipart helpers and a test. For
`sympy__sympy-13031`, Jev brings the sparse matrix's `row_join()` into fifth
place, while 4B keeps generic matrix methods above the sparse implementation.
Conversely, on `nushell__nushell-13831`, Jev spends early positions on related
row-splitting code, while 4B prioritizes the target column-splitting functions.
Neither model wins every instance.

## Cost and failures

| Stage, across 100 issues | Requests | Recorded API cost | Failures |
| --- | ---: | ---: | --- |
| Sonnet grep planning | 162 attempts | $1.003761 | 62 initial HTTP 429; 100 final plans available |
| BM25 top-100 → Jev | 1000 | $0.288725 | 14 HTTP 403, affecting 5 issues |
| Grep → Jev | 221 | $0.045361 | 1 HTTP 403, affecting 1 issue |

All successful Jev calls resolve to `typesafe/jev-1.13-20260917`. The 403
responses contain the upstream `typesafe.ai` Cloudflare marker. Their exact
trigger is unknown; no content was rewritten to bypass them. Production
fallback ordering is included in quality results. Failed-call costs without
reported usage remain unknown, rather than being counted as free.

For one deployed pass over 100 issues, the recorded planner-plus-Jev cost
would be about **$1.049**, versus **$0.289** for BM25-plus-Jev. The experiment
reuses one frozen plan per issue across all three grep rerankers, so planning
is billed once rather than three times. Local GPU costs are not priced here.

The 530 generated grep actions include one invalid NUL-matching regex in
`jqlang__jq-2235`, retained as an action error, and one action reaching the
500-line cap. `babel__babel-13928` retrieves no candidates and receives zero
recall. These cases are retained without BM25 refill or prompt repair.

## Ground-truth coordinate audit

The dataset producer's
[`GTLocator.compare_symbols`](../../codenib/dataset/gt_locate.py) retains
**post-patch** chunks for modified symbols and pre-patch chunks for deletions.
The original report and initial progress figures compared those published
ranges directly with base-commit candidates. Overlap tolerates small shifts,
but it can credit neighboring code or miss the intended definition.

The audit resolves all **151** published symbols at their exact base commit,
using the same default unsplit chunker as `GTLocator`. **119 ranges across
77 instances change.** One Tokio `remove()` name resolves twice; the audit
uses the producer's existing last-wins dictionary behavior and records both
alternatives.

Queries, candidates, plans, scores, and ranking order are unchanged. No model
is called again. This is a coordinate correction, not a relabeling of which
symbols fix the issue. It does not resolve possible semantic label errors or
training-set contamination in historical public issues.

| Reranker | Published BM25 R@5 | Base-aligned BM25 R@5 | Published grep R@5 | Base-aligned grep R@5 |
| --- | ---: | ---: | ---: | ---: |
| Qwen 0.6B | 48.82% | 48.40% | 64.93% | 62.48% |
| Qwen 4B | 59.00% | 58.30% | 69.73% | 68.07% |
| Jev | 59.72% | 59.35% | 73.57% | 71.40% |

The published-frame Jev-versus-4B grep interval was [+0.49, +7.83] pp.
After alignment it crosses zero. Thus the initial claim of a resolved
Jev-over-4B advantage on grep is withdrawn; the retrieval-route benefit
remains supported. File metrics and all timing/cost measurements are unchanged.

## Retrieval coverage

Coverage is macro target-block recall over every raw candidate, before
order-dependent overlap deduplication. It is the candidate pool's ceiling,
not the final Recall@5. Every issue contributes equally.

| BM25 candidate K | Base-aligned coverage | Published coverage | Issues without a base-aligned target |
| ---: | ---: | ---: | ---: |
| 50 | 54.25% | 55.87% | 42 |
| 100 | 63.78% | 64.40% | 32 |
| 200 | 70.78% | 72.20% | 24 |
| 500 | 81.65% | 84.77% | 14 |
| 1000 | 90.80% | 93.50% | 7 |

The new top-50 prefixes match the earlier experiment's candidate identities,
content, and ordering on all 100 instances. Rank placeholder scores differ
only by a constant offset. The full-corpus audit uses the same 3,000-character
visible spans and covers 100% of the published blocks; all named targets also
resolve at base commit in the separate coordinate audit. File-label coverage has a separate
95.5% ceiling: the published file targets also include filtered declarations,
unsupported grammar files, and ancillary files such as `.d.ts`, `.y`, JSON,
and `CHANGELOG.md`. The code-block labels are unchanged.

The model-grep pool has **75.83%** base-aligned target-block coverage and
85.00% file coverage. Nineteen issues have no base-aligned target block, compared with 32 for BM25
top-100. The grep pool contains a mean of **17.47** chunks, median 13,
p95 52.25, and maximum 100; one issue has no candidates. It recovers at
least one base-aligned block for 19 issues entirely missed by BM25, but also entirely
misses six issues where BM25 has a target. This is complementary retrieval,
not a superset.

For 13 of the 19 rescued issues, BM25 already covers every target file in its
top-100 pool. Thus much of this recovery concerns choosing the right functions
inside known files, rather than discovering an entirely missing file.

A post-hoc round-robin merge of BM25 and grep, capped at 100 unique chunks,
has **83.65%** base-aligned target-block coverage (84.90% in the published
frame). **This merge was not reranked**; its
coverage is not a final Recall@5 result. No model in this experiment scores
the 1000-candidate pool.

## Method

`scripts/benchmark_jev_base.py prepare --coverage-ks ...` freezes the larger
BM25 pool and its coverage curve. `prefix` derives the exact first 100 entries
without rerunning retrieval or changing tie order. The model sweeps have one
observation per issue and model. The earlier top-50 results have two repeats;
their latency measurements were taken earlier and are not an interleaved
serving benchmark.

The dedicated arms are Qwen3-Reranker-0.6B and Qwen3-Reranker-4B, using the
same pinned revisions, H100, BF16, batch size 8, and last-position output head
as the previous report. The input limit is 12,288 tokens; all 10,000 BM25 pairs
fit, with a maximum of 9,357 including the prompt template. Jev uses
`typesafe/jev-1.13`, ten candidates per sequential request, and zero API
retries. Candidate K=100, request batch size=10, and metric cutoff k=5 are
three distinct controls.

The 1,747 grep pairs also fit, with a maximum of 8,547 tokens. A long issue
still matters for pointwise rerankers: `prometheus__prometheus-11859` has
14,854 issue characters, and Qwen 4B takes 79.15 seconds over its 100 BM25
candidates. Peak allocated GPU memory for that arm is about 13.92 GiB.
Batching bounds memory, but it does not remove the repeated query computation.

The grep planner uses
[Sonnet 4.6 through OpenRouter](https://openrouter.ai/anthropic/claude-sonnet-4.6),
matching the repository's agent default model family. It receives the issue,
repository name, and source-directory counts capped at 6,000 characters. It
receives no patch, hints, target files, target blocks, or retrieved code.
One structured response supplies at most six regex/glob/case-sensitivity
actions, with temperature 0, reasoning disabled, and a 1,000-token output cap.

The executor materializes normalized regular Git blobs into temporary source
trees; it never resets or reads source from the dirty dataset checkouts.
`rg` runs with an argument vector, without a shell. Each action returns at
most 20 matching lines per file and retains the first 500 matching lines in
path/line order. Each match maps to the smallest enclosing **visible** chunk
from the same production chunking policy. The executor interleaves the six
action lists, removes duplicate chunks, and keeps at most 100. It does not
refill from BM25. Invalid regexes and empty retrieval remain observable.

An initial four-worker planning run completed 38 requests and received 62
HTTP 429 responses. Its successful plans were frozen and reused; the failed
plans were resumed serially. Retry support honors `Retry-After`, uses bounded
attempts, and retains all attempt traces. The quality evaluation uses the
resulting frozen plans. Final-call planning latency excludes the initial
run's failed requests and offline resume scheduling, so it is not a measured
production availability or end-to-end latency claim.

All reported total-stage latencies add each issue's separately measured
planning/search/rerank times. They exclude Git snapshot preparation, chunking,
BM25 index construction, model loading, and artifact I/O. A separate top-100
BM25 control search verifies exact candidate agreement and measures search
on an already-built index; the top-1000 preparation time is not substituted
for this control.

The labels and metric definitions are the same as the previous report:
1-based gold blocks, visible candidate spans, unique files, and first-wins
non-overlapping spans before final ranking cutoffs. Confidence intervals use
5,000 paired repository-cluster bootstrap draws. They are exploratory and
not corrected for multiple comparisons.

## Reproduction and artifacts

The raw run root is `/mnt/data/zhongming/`, with prefix
`codenib-jev-base-20260923-`. Frozen inputs are `k1000-v1`, `k100-v1`, and
`grep100-v2`. Model outputs use `k100-{qwen06,qwen4,jev}-v1` and
`grep100-{qwen06,qwen4,jev}-v1`. Each includes configuration, per-observation
rankings, timings, and reported usage. Jev outputs also include every typed
answer and failed-call trace. Grep inputs include all generated plans and
their previous failed attempts. The interrupted `grep100-v1` planning run
and one-case pilot are retained separately; the pilot is excluded from all
reported quality and cost aggregates.

Start from new output directories. Load `OPENROUTER_API_KEY` through the
existing shell configuration without printing it. A clean serial planning
run does not require the recorded experiment's `--plans-from` recovery step.

```bash
export EVAL_ROOT=/path/to/new/jev-candidates
export BASE_REPOS=/path/to/base-repositories
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1

python scripts/benchmark_jev_base.py prepare \
  --prebuilt-root "$BASE_REPOS" --output "$EVAL_ROOT/k1000" \
  --candidates 1000 --coverage-ks 50 100 200 500 1000
python scripts/benchmark_jev_base.py prefix \
  --prepared "$EVAL_ROOT/k1000" --output "$EVAL_ROOT/bm25" --candidates 100
python scripts/benchmark_model_grep.py \
  --prepared "$EVAL_ROOT/bm25" --prebuilt-root "$BASE_REPOS" \
  --output "$EVAL_ROOT/grep" --workers 1 --live --max-cost-usd 3

# Pinned Qwen snapshots must already be present in the HF cache.
for route in bm25 grep; do
  for size in 0.6B 4B; do
    python scripts/benchmark_jev_base.py run \
      --prepared "$EVAL_ROOT/$route" --output "$EVAL_ROOT/$route-$size" \
      --backend qwen --model "Qwen/Qwen3-Reranker-$size" --repeats 1
  done
  python scripts/benchmark_jev_base.py run \
    --prepared "$EVAL_ROOT/$route" --output "$EVAL_ROOT/$route-jev" \
    --backend jev --model typesafe/jev-1.13 --repeats 1 --live --max-cost-usd 1
done

python scripts/analyze_jev_routes.py \
  --bm25-prepared "$EVAL_ROOT/bm25" --bm25-sweep "$EVAL_ROOT/k1000" \
  --grep-prepared "$EVAL_ROOT/grep" \
  --bm25-runs "$EVAL_ROOT/bm25-0.6B" "$EVAL_ROOT/bm25-4B" "$EVAL_ROOT/bm25-jev" \
  --grep-runs "$EVAL_ROOT/grep-0.6B" "$EVAL_ROOT/grep-4B" "$EVAL_ROOT/grep-jev" \
  --output "$EVAL_ROOT/results.json" --case-csv "$EVAL_ROOT/cases.csv"

python scripts/audit_jev_base_labels.py \
  --prebuilt-root "$BASE_REPOS" \
  --bm25-prepared "$EVAL_ROOT/bm25" --grep-prepared "$EVAL_ROOT/grep" \
  --bm25-sweep "$EVAL_ROOT/k1000" \
  --runs "$EVAL_ROOT/bm25-0.6B" "$EVAL_ROOT/bm25-4B" "$EVAL_ROOT/bm25-jev" \
    "$EVAL_ROOT/grep-0.6B" "$EVAL_ROOT/grep-4B" "$EVAL_ROOT/grep-jev" \
  --results "$EVAL_ROOT/results.json" --case-csv "$EVAL_ROOT/cases.csv"
```

This implementation is an experiment script, not a new default product route.
The Jev reranker consumes ordinary `NodeInfo` candidates from either retrieval
method. The production BM25 example in [Jev configuration](../jev.md) explicitly
sets both the retrieval stage and rerank candidate cap to 100.

Validation: 25 focused experiment tests pass, including immutable snapshot
reads, candidate-prefix integrity, truncated-span credit, grep action handling,
rate-limit trace retention, finite cost caps, resumed-attempt accounting, and
base-commit label alignment. Formatting,
lint, namespace, and artifact integrity checks also pass. The existing Jev
integration's 179 targeted checks passed in the preceding implementation phase.
