# Contextual BM25 development pilot

Adding generated file context moved the expected file into the top five for
4 of 10 existing CodeNib questions, compared with 3 of 10 for ordinary BM25.
A deterministic context baseline also reached 4 of 10. At the same 8,000-token
source-evidence limit, generated context and ordinary BM25 both covered 9 of
10. This small development pilot supports an optional community experiment;
it does not justify changing the default or claiming a general retrieval gain.

The [implementation and boundaries](contextual_retrieval.md) describe how to
use the feature. The [machine-readable receipt](contextual_retrieval_pilot.json)
retains the configuration, selected cases, per-case measurements, usage and
artifact digests. This is an excluded experiment log, not a supported benchmark.

## What was compared

All three arms used the same pinned CodeNib source, 1,586 production chunks,
BM25 tokenizer, candidate limit and returned source text:

| Arm | Ranking context |
| --- | --- |
| B0: current BM25 | Existing chunk text and metadata. |
| B1: deterministic context | Module docstring/imports plus nearby definition signatures and docstrings, capped at 1,024 characters; no model. |
| B2: generated context | A short explanation from the complete target function/method and bounded beginning of its file, capped at 1,024 characters. |

Context was added to 1,415 function/method chunks in both enriched arms.
Headers and epilogues stayed unchanged. Generated context had 521,568 total
characters; deterministic context had 1,408,404. The arms share a prefix cap,
not equal prefix lengths or indexing-token costs. Source-evidence limits are
equal. No queries, expected paths or answer labels entered generation.

The corpus is seven complete backend subtrees at commit
`26be3c4819b20280c2098e38d0326f13970f6c22`: `codenib/agent`, `compiler`,
`dataset`, `index`, `llm`, `model`, and `ops`. The production chunker processed
140 Python files; 119 had eligible contextualized targets. Full-package
preparation found 8,838 chunks and 8,068 eligible targets, so scope was narrowed
before inspecting retrieval outcomes to fit the approved $2 pilot.

## Results and practical interpretation

The ten natural-language cases are existing published development questions
from `docs/experiments/jev_retrieval_cases.json`, selected by backend topic.
Each has one expected file. “Found” means that file appears, even if the
returned function is not the one that answers the question.

| Measurement | Current BM25 | Deterministic context | Generated context |
| --- | ---: | ---: | ---: |
| Expected file in top five, 10 questions | 3/10 | 4/10 | 4/10 |
| Expected file in 8,000-token packet, 10 questions | 9/10 | 7/10 | 9/10 |
| File reciprocal rank at five, mean | 0.250 | 0.300 | 0.283 |
| Exact function first, 12 symbol-name controls | 8/12 | 7/12 | 10/12 |
| Exact function in top five, 12 symbol-name controls | 12/12 | 12/12 | 12/12 |

The exact-symbol controls were sampled deterministically from public symbol
metadata with the recorded seed, separately from the natural-language cohort.
They are easy development controls, not twelve extra realistic user questions.
Do not combine the cohorts to headline an improvement.

Generated explanations can help some descriptions surface earlier. The line
conversion question moved its expected file from rank 12 to rank 3. However,
that first matching chunk was `GTLocator.get_changed_line_ranges()`, which
parses diff hunks. The file-level improvement does not demonstrate correct
function retrieval or that the answer is present in that chunk.

The fixed-budget outcomes also show a tradeoff. Generated context recovered
`codenib/agent/history.py` for the bounded-history question within the evidence
packet, while ordinary BM25 missed it. It lost
`codenib/index/embedding/artifact_integrity.py` from the vector-integrity
packet, although that file's first candidate rank stayed at 19. Changes in
earlier source-chunk sizes and ordering affect what fits. The net result was
still 9/10. Deterministic context alone moved the reranker-fallback file from
rank 8 to rank 2; generated context put it at rank 10.

Use generated context as an opt-in representation to evaluate on a workload
where code descriptions may help. These results give no reason to pay for it
when ordinary BM25 already supplies sufficient evidence, or to prefer it over
cheap context without a workload-specific comparison. Correct function/line
labels, necessary supporting code, downstream answer correctness, other
repositories/languages and held-out queries remain untested. No statistical
or research-novelty claim is made from this sample.

## Cost and execution

The pilot used explicitly configured `anthropic/claude-haiku-5-5`, adaptive
thinking at low effort, a 512-token output limit, omitted temperature, and a
75-word instruction. These are study settings; the production feature has no
model-name special case and selects no provider. Set configuration temperature
to `None` when the selected provider requires omission, as described in the
[provider migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide).

There were 1,415 main responses, zero API errors, and one earlier smoke
response: 1,416 paid calls in total. Reported usage including that smoke was
8,048,472 input tokens and 181,372 output tokens. Input included 8,042,808
five-minute cache-write tokens, 5,664 uncached tokens and no cache reads. All
main responses finished normally and passed the corrected production prefix
validator. The main generation took about 239 seconds with eight workers;
this is not a production serial-build latency measurement.

At the provider's recorded short-context rates, the token-priced estimate is
**$1.0966034** including the smoke. The conservative meter charged all reported
input at the more expensive cache-write ceiling and recorded **$1.7003804**,
below the **$2** authorization. Neither figure is an invoice reconciliation.
The [pricing reference](https://platform.claude.com/docs/en/about-claude/pricing)
and explicit rates are retained with the receipt.

Saved uncompressed index artifacts were 2,119,059 bytes for ordinary BM25,
5,113,515 for deterministic context and 3,828,501 for generated context.
These are artifact sizes, not peak memory. Warm ranking medians on the ten
questions were approximately 1.15, 1.22 and 1.26 milliseconds respectively,
using ten repeated calls per question. This excludes source reads and model
generation, is machine-specific, and is not an end-to-end speed claim.
Search makes no contextualization calls.

## Reproduction and retained failures

`scripts/benchmark_contextual_retrieval.py` separates source preparation,
paid generation, replay through the production builder, and scoring. To run
a new study, copy the receipt's `config` object into a JSON file, then run:

```bash
python scripts/benchmark_contextual_retrieval.py prepare \
  --repo /path/to/CodeNib --ref 26be3c4819b20280c2098e38d0326f13970f6c22 \
  --subdir codenib/agent codenib/compiler codenib/dataset codenib/index codenib/llm codenib/model codenib/ops \
  --cases docs/experiments/jev_retrieval_cases.json \
  --config /path/to/config.json --output /path/to/new-study
# Paid step: requires explicit model, pricing, budget and a private credential file.
python scripts/benchmark_contextual_retrieval.py generate \
  --study /path/to/new-study --credential-file /path/to/private-key --workers 8
python scripts/benchmark_contextual_retrieval.py build-generated --study /path/to/new-study
python scripts/benchmark_contextual_retrieval.py score --study /path/to/new-study --arms B0 B1 B2
```

For a new study, remove the previous run's `initial_spent_ceiling_usd` and
protocol-amendment note from the copied config, or deliberately retain them
when carrying its cost forward. Provider rates and constraints must be checked
again before generation. Reproduction with fresh responses can differ.
The production builder itself does not enforce a dollar cap; the pilot CLI's
reservation meter is a separate experiment safeguard.

The first source-only smoke returned 1,193 characters, exceeding the 1,024
limit. A 75-word instruction was added before any retrieval outcomes were
inspected; the failed smoke and its cost were retained. The main run produced
one apparent rejection because the original validator interpreted an inline
` ```json ` mention as a fenced code block. The validator now rejects actual
fence lines, including tilde fences, while allowing prose about syntax. The
same 1,415 saved responses were replayed through the production builder, with
no replacement responses or new calls to obtain better text.

Generation records and their reservation ledger are append-only, and a saved
response is never regenerated merely because validation failed. An unresolved
reservation keeps its full ceiling on resume. A single process owns the study's
generation lock; its next invocation owns recovery. HTTP retry attempts are
disabled in the client; any CLI retry of a transient error retains the earlier
attempt and its conservative cost. No transient retry was needed here.

The snapshot is checked against immutable Git blobs. All three arms were
verified to have identical document text and source metadata except the labeled
context field. Final scoring verifies source bytes before and after reads,
and packs exact source excerpts with the same 8,000-token `cl100k_base` limit,
128 candidates, exact-span deduplication and skip-if-oversize rule. It measures
retrieval/evidence localization without asking a model to answer the queries.

The original manifest hashed the upstream cases file. Digests for the selected
cases and request file, plus comparison-arm checks, were added after generation
and the first exploratory score. Exact prompt replay and a second scoring run
reproduced every non-timing outcome. This is an audit improvement, not a claim
that the pilot was prospectively preregistered. Full local prompt, response,
ledger and score artifacts are retained by digest; the compact receipt omits
private source paths and provider rate-limit headers.

## Next decision

Keep the community method optional and experimental. Before adding dense or
graph-conditioned context, label correct functions and supporting spans on
held-out repository tasks and compare the same token budgets against ordinary
BM25 and cheap context. Promote a more expensive representation only if it
helps those tasks enough to justify generation, rebuild and storage costs.
Keep the December 10, 2026 support/narrowing/removal review from the feature log.
