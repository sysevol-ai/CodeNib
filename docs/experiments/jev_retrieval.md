<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Can Jev replace embedding recall?

Measured locally on 2026-09-22 (America/Los_Angeles), using main commit
`61a9ab2fd2cc8f656fa541d6891289f30531f2e7` plus the Jev integration in this
checkout. The online sweep started at 2026-09-23 04:44:36 UTC.

**BM25 plus Jev works without embeddings, but did not save warmed online
latency on this H100 host.** Dense retrieval took 21 ms at the median, BM25
took 38 ms, and either retrieval route plus Jev took about 590 ms. Jev improved
BM25's ranking in this small sample, while inheriting its candidate omissions.
Sending 200 chunks directly to Jev took 6.26 seconds with the current sequential
batch implementation.

This is an exploratory comparison of retrieval components, not an evaluation
of full Ask answers or a reason to change the default retrieval route.

For a matched comparison of complete retrieval-plus-reranker paths, see the
[100-issue dense and hybrid controls](jev_dense.md). That experiment uses
CodeNib Base, code-block Recall@5, and a candidate budget of 100; its quality
numbers must not be mixed with this 15-question, file-group exploration.

## Method

- A frozen corpus of 7,013 Python chunks from 447 files under `codenib/`, built
  with the production tree-sitter chunker at depth 2 and at most 100 lines per
  chunk. Each chunk's content is capped at 3,000 characters for every arm.
- Fifteen questions with expected file groups, fixed before the sweep: three
  from the existing Ask retrieval gate and twelve authored exploration cases.
  See [the query set](jev_retrieval_cases.json). These are not held-out labels.
- Both retrieval routes return twenty chunks. The bare route returns its first
  five; the Jev route reranks those same twenty and returns five. Timed source
  materialization uses the same in-memory corpus snapshot for both routes.
- BM25 uses `BM25CodeIndexer`. Dense retrieval uses the production embedding
  wrapper with `nomic-ai/CodeRankEmbed`, revision
  `3c4b60807d71f79b43f3c4363786d9493691f8b1`, CUDA on an NVIDIA H100 PCIe,
  a 1,024-token limit, normalized vectors, and an in-memory FAISS `IndexFlatIP`.
  The model's registered query prompt is retained. FAISS, OMP, and MKL use one
  CPU thread.
- Jev uses the production `RerankAgent`: two sequential requests of ten
  independent, four-level relevance questions. Requested model
  `typesafe/jev-1.13`; every successful response resolved to
  `typesafe/jev-1.13-20260917`. Network time, HTTP setup, response validation,
  and score sorting are included in rerank latency.
- Both retrieval routes are warmed before timing. Every question is repeated
  twice, with seeded query and arm ordering. There are thirty observations per
  arm, but only fifteen distinct questions. The bare and reranked arm share
  each retrieval observation; their latency samples are paired.
- API retries are disabled for the experiment. Failed calls remain in the
  results; the final sweep uses `--continue-on-error` to measure the production
  fallback behavior without silently treating a fallback as successful scoring.

Coverage is the fraction of a question's expected file groups represented in
the selected chunks, averaged equally over questions and repeats. Duplicate
chunks from one file do not increase coverage. Success requires every expected
group; MRR is the reciprocal rank of the first expected file. These are file
localization metrics, not measures of function correctness or answer quality.

## Measured results

| Route | Candidate coverage @20 | Coverage @5 | All-group success @5 | MRR @5 | Online p50 | Online p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BM25 | 66.7% | 37.8% | 26.7% | 0.413 | 38 ms | 57 ms |
| BM25 + Jev | 66.7% | 58.9% | 53.3% | 0.506 | 594 ms | 699 ms |
| CodeRankEmbed + FAISS | 64.4% | 51.1% | 40.0% | 0.367 | 21 ms | 32 ms |
| CodeRankEmbed + FAISS + Jev | 64.4% | 57.8% | 46.7% | 0.553 | 590 ms | 689 ms |

The BM25 + Jev row includes two partially failed reranks, described below.
The 1.1 percentage-point difference between the two Jev routes is insufficient
to establish a quality winner on fifteen exploratory questions.

Median query embedding time was 17.0 ms, including host-side encoding work and
GPU synchronization. Median Jev reranking time was 545 ms for BM25 candidates
and 568 ms for dense candidates; one successful Jev batch took a median 279 ms.
Medians of separate phases need not add to the median total.

Offline costs were 1.57 seconds for chunking, snapshot creation, and BM25
construction, an additional 4.97 seconds to load the cached embedding model,
and 33.80 seconds to encode documents and construct FAISS. These costs are
excluded from online timings. A deployment that frequently starts from an
unindexed repository could benefit from avoiding this dense-index build even
though its warmed queries were faster here.

### Candidate omissions and ranking changes

| Case | BM25 coverage @20 | Dense coverage @20 | BM25 @5 | BM25 + Jev @5 | Dense @5 | Dense + Jev @5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Stale semantic lifecycle | 100% | 50% | 50% | 50% | 50% | 50% |
| Model configuration | 100% | 67% | 67% | 33% | 67% | 67% |
| Wiki grounding | 100% | 50% | 50% | 100% | 50% | 50% |
| Bounded history | 0% | 100% | 0% | 0% | 100% | 100% |
| Provider retry | 0% | 0% | 0% | 0% | 0% | 0% |
| Prompt cache | 100% | 100% | 100% | 100% | 100% | 0% |
| Vector integrity | 0% | 100% | 0% | 0% | 100% | 100% |
| Graph expansion | 100% | 100% | 0% | 100% | 100% | 100% |
| Rank fusion | 100% | 100% | 100% | 100% | 100% | 100% |
| Keyword extraction | 100% | 100% | 100% | 100% | 0% | 100% |
| Line conversion | 0% | 0% | 0% | 0% | 0% | 0% |
| Embedding trust | 100% | 100% | 100% | 100% | 0% | 100% |
| Repository exclusions | 100% | 0% | 0% | 100% | 0% | 0% |
| Wiki transactions | 100% | 100% | 0% | 100% | 100% | 100% |
| Rerank fallback | 0% | 0% | 0% | 0% | 0% | 0% |

For example, BM25 omitted the expected history and vector-integrity files from
its entire candidate set. Jev cannot recover those files from the supplied
state. Conversely, BM25 found repository-exclusion code that dense retrieval
missed. Jev also demoted useful candidates for model configuration and prompt
caching, so its benefit is not uniform.

As a post-hoc diagnostic, the union of the two top-twenty sets covers 80.0% of
expected groups. That union contains up to forty chunks: it is not a measured
hybrid arm, and this number makes no claim about its latency or final ranking.

### Scanning code directly with Jev

A separate scaling probe sends a fixed 200-chunk subcorpus directly to the
decision reranker, without BM25 or dense retrieval. It takes twenty sequential
requests, **6,260 ms**, and **$0.00352821** of reported usage, with no failures.

The sample deliberately contains one chunk from each expected file group for
the first question, plus seeded random distractors. It is a latency probe;
its quality score is not an unbiased recall measurement. The entire 7,013-chunk
repository was not scanned. This result concerns the current ten-candidate,
sequential implementation and does not establish a lower bound for other
batching or concurrency policies.

### Availability and reported cost

The final run made 140 HTTP requests: 120 for the four-arm comparison and twenty
for the scan. Of those, 138 succeeded, reporting 704,423 input tokens, 22,122
output tokens, and **$0.029585766** in usage cost. The comparison alone reported
$0.026057556. Failed responses supplied no usage, so their cost is unknown.
Earlier pilot and diagnostic requests are excluded from these totals.

Two requests failed with HTTP 403, both the second BM25 batch for the Wiki
transaction question, once per repeat. The error body contains a Cloudflare
block page for `typesafe.ai`; an identical diagnostic request also failed.
Other batches continued successfully. The specific triggering rule is unknown;
the payload contains ordinary repository source, including SQL-related code.
No input rewriting or special retry policy was used to work around the block.

Those two observations retain the first successful batch's scores and the
existing fallback for unscored candidates. Their expected publish method was
in the successful batch, so file coverage remained 100%; that does not mean
both scoring requests succeeded. This is a deployment limitation for the
current hosted route even where the headline quality metric looks unaffected.

Live testing also exposed a client compatibility issue: Jev serializes
probabilities to two decimal places, so a distribution can sum to 0.99 or
1.01. The client now allows half a rounding unit per entry while preserving
the returned distribution and score. A deterministic regression covers this
behavior; malformed distributions still fail validation.

## Decision and limits

Keep Jev as an optional reranker. BM25 plus Jev is a viable route when avoiding
embedding dependencies, GPU residency, or index construction is the priority.
This experiment does not support replacing warmed dense retrieval to save
online latency: the existing local dense search was already faster, and remote
Jev scoring dominated both reranked routes.

Retain candidate recall as a separate concern. Decisions can score code placed
in their state; they do not search repository content absent from that state.
The complementary misses here argue against removing either retrieval source
globally on the strength of this small benchmark.

These results apply to one repository, one H100 host, short Python chunks,
fifteen English questions, and one hosted model version. Expected file groups
are not exhaustive relevance labels. This does not measure CPU-only or remote
embeddings, a production hybrid route, graph expansion, answer generation,
parallel Jev requests, or a larger independently labeled query set.

## Reproduce and inspect

Use an environment with the repository's full dependencies and the pinned
CodeRankEmbed model available locally. Load `OPENROUTER_API_KEY` into the shell,
then run from the repository root. This command makes billed API calls:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  python scripts/benchmark_jev_retrieval.py \
  --live --continue-on-error --repeats 2 --scan-size 200 \
  --output /tmp/codenib-jev-retrieval-new-run
```

The output directory must not already exist. Without `--continue-on-error`,
the script stops after the first failed rerank and keeps partial results for
diagnosis. The default usage guard stops further comparisons once recorded
cost reaches $0.25; it cannot predict the next request's charge or unknown
failed-request charges. A new run can resolve to a different backend revision,
so inspect the recorded response models.

The [benchmark script](../../scripts/benchmark_jev_retrieval.py),
[fixed cases](jev_retrieval_cases.json), and
[compact results](jev_retrieval_results.json) are reviewable in the checkout.
The results include per-case metrics, implementation and artifact hashes,
runtime package versions, failure records, and the original run configuration.
Frozen chunk snapshots, per-query rankings, and model answers remain locally at:

```text
/mnt/data/zhongming/codenib-jev-retrieval-20260922-main-v3/
```

The retained `pilot-v1`, `main-v1`, and `main-v2` sibling directories are smoke
or interrupted diagnostic runs and are not pooled into the final comparison.
