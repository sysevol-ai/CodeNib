<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Jev versus dedicated rerankers on CodeNib Base

This experiment evaluates the entire Hugging Face
[CodeNib Base test split](https://huggingface.co/datasets/fishmingyu/codenib-base-dataset),
using main commit `61a9ab2fd2cc8f656fa541d6891289f30531f2e7` plus this checkout's
Jev integration. It measures issue-to-code localization after a shared retrieval
stage. The earlier [local retrieval experiment](jev_retrieval.md) used fifteen
authored questions about this repository; its numbers are a separate experiment.

The [candidate-budget follow-up](jev_candidates.md) expands the recall audit,
compares rerankers at K=100, and evaluates model-planned grep candidates.
It also identifies post-patch coordinates in modified-symbol labels and adds
a base-commit alignment audit. The numbers on this older page retain the
published-coordinate metric for reproducibility; use the follow-up's aligned
results for the current comparison.

**Jev is competitive with the 4B and 8B rerankers on this candidate pool, at
lower observed latency; the 0.6B reranker is faster and less accurate.** Jev
reaches 52.64% span Recall@5 at a 1,481 ms median, versus 51.63% / 2,570 ms
for Qwen 4B and 52.05% / 3,600 ms for Qwen 8B. The small Recall@5 differences
against those larger models are not statistically resolved. Qwen 0.6B reaches
45.15% at 848 ms. Hosted availability is a separate difference: 18 of 1,000
Jev requests failed with HTTP 403, affecting ten query observations.

The formal scoring sweep began at 2026-09-23 05:16 UTC, September 22 in the
host's America/Los_Angeles timezone. There are **800 rerank observations**:
100 instances, four rerankers, and two repeats. See the
[summary and paired statistics](jev_base_results.json) and
[all 100 per-instance comparisons](jev_base_cases.csv).

## Results

These are incremental rerank latencies. Retrieval, index construction, and
model loading are excluded; the BM25 row performs no reranking. Quality uses
the file/span deduplication rules described below.

| Reranker | Span Recall@5 | File Recall@5 | Span MRR | Rerank p50 | Rerank p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BM25 order | 29.08% | 48.83% | 0.216 | — | — |
| Qwen3-Reranker-0.6B | 45.15% | 65.50% | 0.381 | 848 ms | 1,804 ms |
| Qwen3-Reranker-4B | 51.63% | 71.83% | 0.479 | 2,570 ms | 5,397 ms |
| Qwen3-Reranker-8B | 52.05% | 72.67% | 0.505 | 3,600 ms | 7,369 ms |
| Jev 1.13 | 52.64% | 74.17% | 0.532 | 1,481 ms | 2,222 ms |

Jev's median is 42% below Qwen 4B and 59% below Qwen 8B, but 75% above
Qwen 0.6B. These are comparisons of the measured serving configurations,
including network time for Jev, rather than intrinsic model speed claims.

At the first result, span recall is 25.33%, 33.17%, 37.87%, and 41.00% for
0.6B, 4B, 8B, and Jev respectively. Jev's difference against 4B is +7.83
percentage points with a paired 95% interval of [+1.23, +16.51]; its difference
against 8B is not resolved. Complete target-block coverage at five results is
41%, 46%, 48%, and 47%, respectively: no model leads on every metric.

On the sixty instances with at least one target block in the candidate pool,
span Recall@5 is 75.25%, 86.06%, 86.75%, and 87.74%, respectively. The other
forty instances contribute zero for every reranker. Improving candidate recall
therefore remains a larger open opportunity than the observed 4B/8B/Jev
differences.

### Paired differences

Differences below are **Jev minus the comparison**, in percentage points of
span Recall@5, with repository-cluster bootstrap intervals. Win/tie/loss
counts average each instance's two repeats first and count 100 distinct tasks.

| Comparison | Mean difference | Paired 95% interval | Jev wins / ties / losses |
| --- | ---: | ---: | ---: |
| BM25 order | +23.56 pp | [+17.02, +30.62] | 30 / 70 / 0 |
| Qwen 0.6B | +7.49 pp | [+1.04, +15.65] | 11 / 87 / 2 |
| Qwen 4B | +1.01 pp | [−1.57, +3.95] | 5 / 92 / 3 |
| Qwen 8B | +0.59 pp | [−2.38, +3.92] | 4 / 92 / 4 |

Forty ties are forced by missing candidate targets. Among the sixty reachable
instances, Jev versus 4B is 5 / 52 / 3, and versus 8B is 4 / 52 / 4. This is
evidence of comparable Recall@5 on this sample, not proof that their underlying
quality is identical. The intervals are exploratory, not adjusted for testing
multiple metrics and model pairs.

Qwen 4B improves over 0.6B by 6.48 points, with interval [+1.04, +12.97].
Increasing 4B to 8B adds 0.42 points, with interval [−0.73, +1.75], while
increasing median latency by 40%. This sweep does not establish a Recall@5
benefit from that size increase.

### Language breakdown

Every number below is macro span Recall@5, except the all-candidate coverage
column. Subgroups contain only 19–21 instances and differ in candidate recall;
they should not be interpreted as isolated language ability measurements.

| Language | Instances | Candidate coverage | BM25 | Qwen 0.6B | Qwen 4B | Qwen 8B | Jev |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| C++/C | 20 | 57.50% | 30.42% | 45.42% | 53.33% | 52.92% | 54.38% |
| Go | 21 | 38.10% | 23.81% | 35.71% | 35.71% | 38.10% | 35.71% |
| Python | 20 | 58.33% | 30.00% | 58.33% | 58.33% | 58.33% | 58.33% |
| Rust | 20 | 65.83% | 35.00% | 52.83% | 57.33% | 57.33% | 56.33% |
| TypeScript/JavaScript | 19 | 60.70% | 26.32% | 33.33% | 54.39% | 54.39% | 59.65% |

The largest Jev-versus-0.6B difference is in TypeScript/JavaScript. Python's
Recall@5 is tied at the candidate ceiling for all four models. Thirteen of
the 21 Go instances have no target block in the pool, limiting every model.

### What changes in individual cases

These examples illustrate both directions of disagreement. The complete CSV
retains every instance; they are not a separate evaluation subset.

| Instance | Qwen 0.6B | Qwen 4B | Qwen 8B | Jev | Observed ranking difference |
| --- | ---: | ---: | ---: | ---: | --- |
| `axios__axios-6539` | 0% | 0% | 0% | 100% | Jev ranks the target `isAbsoluteURL()` first; the Qwen models prioritize request construction and dispatch code. |
| `redis__redis-13338` | 0% | 33% | 0% | 67% | Jev brings both `streamReplyWithRange()` and `streamEstimateDistanceFromFirstEverEntry()` into the first five spans; all models retrieve the correct file. |
| `sharkdp__bat-3108` | 100% | 50% | 50% | 100% | Jev and 0.6B cover both application configuration and CLI construction; the larger models put more supporting code ahead of the latter. |
| `astral-sh__ruff-15330` | 100% | 100% | 100% | 50% | Jev finds `skip_script_comments()` but leaves the second target, `is_valid_script_line()`, outside the first five spans. |
| `fmtlib__fmt-2310` | 100% | 100% | 100% | 50% | All find `write_nonfinite()`; Jev misses the second target, `specs_setter.on_zero()`, at this cutoff. |

Values are span Recall@5. The Redis example shows why file-only scores can
conceal substantial differences in localization. The Ruff and fmt examples
show that selecting one directly relevant function can still leave a required
companion change out of the context. These are observations of rankings, not
claims about the models' internal reasoning.

### Repeat stability and operational differences

All three local Qwen models return identical scores and rankings across both
repeats for all 100 instances. Jev changes some scores and ranking positions
on 99 instances, but its mean raw top-five set overlap is 96.6%, and full-rank
Spearman correlation is 0.992. Only `jqlang__jq-2598` changes span Recall@5,
from 25% to 50%. Tail reordering should not be confused with widespread
retrieval-quality instability.

The fraction of repeated score values within a fifty-candidate ranking averages
7.10%, 5.96%, 4.82%, and 26.07% for 0.6B, 4B, 8B, and Jev. Equal scores retain
BM25 order, so the first-stage order remains influential. These scores have
different meanings: Qwen uses normalized yes/no token logits; Jev uses an
expected ordinal relevance level. Neither is established here as a calibrated
probability that a particular code block contains the bug.

Peak allocated GPU memory is 5.74 GiB for 0.6B, 13.63 GiB for 4B, and 23.11 GiB
for 8B, including model weights and temporary inference tensors. Jev performs
no local model inference. Its 982 successful responses report 7,037,296 input
tokens, 159,068 output tokens, and **$0.295566432** of usage cost across the
200 observations. Pilot calls and unknown charges for failed responses are
excluded; local GPU ownership or rental cost is not estimated.

All 18 failures are HTTP 403 responses with a Cloudflare block-page marker for
`typesafe.ai`. They recur on the same five instances in both repeats:

| Instance | Failed requests per repeat | Target-block candidate coverage | Jev span Recall@5 |
| --- | ---: | ---: | ---: |
| `caddyserver__caddy-5870` | 5 / 5 | 0% | 0% |
| `hashicorp__terraform-34814` | 1 / 5 | 0% | 0% |
| `prometheus__prometheus-15142` | 1 / 5 | 0% | 0% |
| `uutils__coreutils-6690` | 1 / 5 | 100% | 100% |
| `uutils__coreutils-6731` | 1 / 5 | 100% | 100% |

The triggering rule is unknown. Inputs were not rewritten or specially retried
to work around the blocks. The aggregate includes the production fallback,
including both completely failed Caddy observations. Rejected requests can
return quickly; restricting latency to the 190 fully successful observations
gives p50 **1,488 ms** and p95 **2,194 ms**, close to the inclusive numbers.

A post-hoc paired check on the same 95 fully successful instances for every
model gives Recall@5 of 45.42%, 52.25%, 52.68%, and 53.31%, respectively.
Jev's differences against 4B and 8B still have intervals spanning zero. These
failures do not account for the headline quality ordering, but the 1.8% request
failure rate and 5% affected-query rate are relevant deployment observations.

## Dataset and candidate construction

The dataset revision is `4eb84e2e8918474969ce68c5b06facf14d6be604`: 100 instances,
25 repositories, and 151 target code blocks. There are 20 Rust, 20 Python,
21 Go, 20 C++/C, and 19 TypeScript/JavaScript instances. Dataset labels come from
the supplied target files and modified/deleted code blocks: 148 modified and
three deleted blocks in this revision. The project's dataset builder selects
representative repositories and difficulty levels from SWE-bench Verified and
SWE-bench Multilingual, then extracts localization labels from patches with
tree-sitter. This run consumes those published labels without regenerating
them. Only the issue's
`problem_statement` is used as the query; hints, patches, and labels are excluded
from model inputs.

Each instance uses its own `base_commit`. Local repository working trees are
not trusted as snapshots: some have later commits or uncommitted changes.
Preparation reads tracked regular source files directly from immutable Git
blobs and leaves every working tree untouched. The production language registry,
path/test/minification filters, and tree-sitter chunker select code, using depth
2 and a maximum of 100 lines per chunk. Corpus sizes range from 180 to 29,493
chunks, with a median of 3,785.

The production BM25 index retrieves fifty chunks using the full issue text.
Every arm receives exactly the same fifty candidates, names, paths, initial
order, and content capped at 3,000 characters per chunk. Visible line spans are
shortened to match truncated content before evaluation. No gold candidate is
injected. Frozen case files carry SHA-256 hashes, and every scoring run verifies
them before loading a model or calling an API. Dense embeddings, graph expansion,
keyword extraction, and answer generation are excluded from this comparison.

The candidate pool covers an average **55.87% of target code blocks**; 40 of the
100 instances have no target block represented at all, 52 have every target
block, and eight have partial coverage. These are first-stage limitations.
Changing the reranker cannot recover code absent from its input. All-candidate
coverage is measured before overlap deduplication, so it is independent of the
reranked order.

## Scoring and timing

| Arm | Fixed model revision | Inference |
| --- | --- | --- |
| Qwen3-Reranker-0.6B | `e61197ed45024b0ed8a2d74b80b4d909f1255473` | Local BF16, H100 PCIe 80 GB |
| Qwen3-Reranker-4B | `22e683669bc0f0bd69640a1354a6d0aebcfeede5` | Local BF16, same GPU |
| Qwen3-Reranker-8B | `77d193c791ed757ca307ee72715aa132723da912` | Local BF16, same GPU |
| Jev | Requested `typesafe/jev-1.13`; all 982 successful responses resolve to `typesafe/jev-1.13-20260917` | OpenRouter Decisions API |

Qwen uses the production issue-localization instruction and pair template,
scoring each `(issue, candidate)` by the softmax of the final yes/no logits.
The benchmark evaluates batches of eight with `logits_to_keep=1` and
`use_cache=False`. Earlier positions' vocabulary logits are unnecessary for
this score. This is an explicit benchmark inference setting; the production
Qwen wrapper is unchanged. A tiny FP32 regression checks equivalence with
full-position logits under left padding. A real BF16 probe found small rounding
differences, so bit-for-bit equivalence is not assumed.

The context limit is 12,288 tokens, selected from input-length inspection before
the formal sweep; the original 8,192 setting would truncate some long issues.
All models receive the full issue. CPU thread counts for OMP and MKL are one.
Local models are loaded once and warmed with a generic query that is not a
benchmark instance. Timing includes tokenization, host/device work, synchronized
GPU inference, and sorting, and excludes model loading.
The environment uses PyTorch 2.9.0, Transformers 4.57.3, NumPy 1.26.4, and
NVIDIA driver 580.173.02. The three model snapshots have identical tokenizer
JSON hashes; all use the same length limit and pair construction.

Jev uses the production decision reranker: five sequential HTTP requests of ten
candidates each. Each candidate has a four-level relevance `score` question;
its expected position on the 0–3 scale, divided by three, determines its rank.
Candidates share state within a request, while the instruction asks for
independent judgments. Network time, request setup, validation, and sorting are
included. API retries are disabled. Failed windows retain the production
fallback and remain counted in both quality and latency; failed API responses
without usage have unknown cost.

Each arm evaluates all 100 instances twice in a seeded shuffled order, for 200
query observations and 10,000 candidate scores per arm. Scoring arms run
sequentially on the host. The repeats estimate variation and are not 200
independent tasks. Local model latency reflects this Transformers configuration,
not an optimized serving system or another GPU. API latency reflects one host
and time window, not a service-level guarantee.

## Metric definitions

- **File Recall@k:** the fraction of target files in the first k distinct files
  after ranking. Several chunks from one file consume one file slot. Target
  files are kept verbatim, including the dataset's non-source targets, so this
  metric also carries source-corpus coverage limitations.
- **Span Recall@k:** the fraction of target code blocks overlapping the first k
  predicted spans, using the project's existing evaluator. Chunk line numbers
  are converted from zero-based to one-based; overlapping predictions are
  deduplicated in rank order. Any overlap with a target block counts as a hit.
- **All targets@k:** the fraction of instances whose complete target-block set
  is covered. **Span MRR:** reciprocal rank of the first overlapping target,
  over the full deduplicated ranking.
- Raw-chunk file coverage is also retained in the machine-readable results,
  since deduplicated file/span cutoffs do not imply identical context budgets.

Results are macro-averaged over instances and repeats. Paired differences first
average each instance's repeats, then use 5,000 repository-cluster bootstrap
draws across the 25 repositories. A confidence interval spanning zero does not
establish a quality winner. Labels describe changed code, not every useful
supporting function; these scores do not measure bug-fix success.

## Scope of the comparison

[Qwen3-Reranker](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) is a dedicated
reranking family with configurable instructions, used here for code. Its
joint query/document scoring uses a decoder model's final yes/no logits.
[Jev](https://openrouter.ai/docs/guides/community/jev) answers typed questions
about supplied state and returns probabilities. It can express relevance with
an application-owned scale, but does not search code missing from that state.

This experiment compares three Qwen sizes with the current Jev integration.
It is not a result for every code-specific reranker, listwise generation model,
prompt, serving engine, or retrieval route. In particular, a BM25-only candidate
pool can favor different rerankers than a dense or hybrid pool. No prompts are
tuned against the test labels.

For this deployment, 0.6B is the fastest measured reranker, while Jev offers
larger-reranker quality without local model residency. Its hosted failures and
different score semantics need to remain visible to the application. Both
Qwen and Jev produce scores without a generated prose answer; the measured
latency difference cannot be explained simply by calling one "non-generative."
The experiment compares these complete scoring policies, including Qwen's
issue-fix instruction and Jev's four-level relevance criteria, rather than
isolating training or model architecture.

This does not establish that embedding recall can be removed without a quality
loss. All arms inherit the same forty unanswerable candidate sets. A different
sparse, dense, graph, or hybrid recall stage would need a separate comparison;
the full repositories were not scanned through Decisions here. Public issue
and repository overlap with model training data is unknown. Parallel Jev
requests, other candidate counts, other GPUs, and optimized local serving
engines are also outside this measurement.

## Reproduce

Use the repository's full Python dependencies, a CUDA GPU with enough memory,
and cached snapshots of the three pinned Qwen models. The source-cache root
must contain `<instance_id>/repo` for every dataset row, with its base commit
present in Git's object database. Preparation does not reset these repositories
or load legacy native indexes.

```bash
python scripts/benchmark_jev_base.py prepare \
  --prebuilt-root "$CODENIB_PREBUILT_DIR" \
  --output /path/to/run/candidates

HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
python scripts/benchmark_jev_base.py run \
  --prepared /path/to/run/candidates \
  --output /path/to/run/qwen06 \
  --backend qwen --model Qwen/Qwen3-Reranker-0.6B --repeats 2
```

Repeat the scoring command with `Qwen/Qwen3-Reranker-4B` and
`Qwen/Qwen3-Reranker-8B`, using separate output directories. With
`OPENROUTER_API_KEY` loaded into the environment, the following command makes
billed requests; its guard uses reported successful-call cost:

```bash
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
python scripts/benchmark_jev_base.py run \
  --prepared /path/to/run/candidates \
  --output /path/to/run/jev \
  --backend jev --model typesafe/jev-1.13 --live \
  --repeats 2 --max-cost-usd 2

python scripts/benchmark_jev_base.py analyze \
  --prepared /path/to/run/candidates \
  --runs /path/to/run/qwen06 /path/to/run/qwen4 \
         /path/to/run/qwen8 /path/to/run/jev \
  --output /path/to/run/results.json \
  --case-csv /path/to/run/case_metrics.csv
```

The analyzer rejects incomplete, duplicated, or differently sourced runs. Each
run retains its configuration, per-observation rankings and metrics, running
summary, and (for Jev) every typed response or error with reported usage.

On the experiment host, raw evidence is under `/mnt/data/zhongming/`, with
the common prefix `codenib-jev-base-20260922-`: `candidates-v1/` contains the
frozen manifest and cases; `qwen06-main-v1/`, `qwen4-main-v1/`,
`qwen8-main-v1/`, and `jev-main-v1/` contain the scoring runs. The adjacent
`environment.json` records dependency versions and production-source hashes.
The committed summary and CSV were generated from these four complete runs;
earlier pilot directories are excluded.

Both experiment-script test files pass all ten local tests, including immutable
Git snapshot selection, input hash validation, line conversion, candidate
coverage under overlapping spans, final-position Qwen scoring, and repeat
comparison. Black, isort, flake8, the repository namespace check, and result
cardinality/manifest consistency checks also pass.
