<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Demo model and cold-start bake-off (2026-08-16)

This note records the production-demo acceptance run after rebuilding all 26
repositories with secure source fingerprints and hybrid BM25/vector/graph
views. The fixed Ask cases are Requests proxy bypass, Gin route registration,
and Vue scheduler flushing. File recall and named-term coverage are
deterministic checks; citation validity requires every returned citation to
carry a real file and positive line range.

## Result

Use the hosted API profile for public generation and retain the local model as
an operator-selectable fallback:

- **Ask:** `deepseek/deepseek-v4-flash` through the DeepSeek API.
- **Wiki:** `deepseek/deepseek-v4-flash` through the DeepSeek OpenAI-compatible
  endpoint.
- **Embeddings:** `Qwen/Qwen3-Embedding-0.6B` through the dedicated local
  endpoint.
- **Local fallback:** `Qwen/Qwen3.6-35B-A3B-FP8` through vLLM, with one MTP
  draft token.

[Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) and
[Muse Glimmer 30B](https://huggingface.co/meta-models/Muse-Glimmer-30B) both
worked, but neither improved the fixed quality signals. Their latency is too
high for the interactive default on this host. Meta's
[Muse Glimmer announcement](https://research.meta.ai/blog/introducing-muse-glimmer-open-agentic-model/)
and the official [GGUF weights](https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF)
remain useful references for a future faster runtime or a background-agent
profile.

## Ask benchmark

| Candidate | Cases | Wall time | File recall | Term coverage | Valid citation ranges | Outcome |
|---|---:|---:|---:|---:|---:|---|
| DeepSeek V4 Flash API | 3/3 | 12.54-12.64s; p50 12.60s | 0.722 | 1.000 | 3/3 | Public default |
| Qwen3.6 FP8, cold repository views | 3/3 | 20.54-29.26s; p50 22.17s | 0.722 | 1.000 | 3/3 | Baseline cold path |
| Qwen3.6 FP8, warm repository views | 3/3 | 19.62-23.54s; p50 21.63s | 0.722 | 1.000 | 3/3 | Keep as local fallback |
| Qwen3.8-27B, standard | 3/3 | 98.41-161.21s; p50 136.69s | 0.722 | 0.889 | 3/3 | Reject on latency and lower term coverage |
| Qwen3.8-27B, MTP=3 | 1/1 Requests | 42.09s | 1.000 | 1.000 | 1/1 | Still 1.8x slower than warm Qwen3.6 Requests |
| Muse Glimmer 30B Q4_K_M, low reasoning | 3/3 | 110.47-150.57s; p50 132.55s | 0.722 | 1.000 | 3/3 | Reject on latency; no quality gain |

The DeepSeek API profile completed Requests, Gin, and Vue in 12.60s, 12.54s,
and 12.64s. It retained the Qwen baseline's 0.722 mean file recall, complete
named-term coverage, and valid citation ranges while reducing median wall time
by about 42% relative to warm Qwen3.6. The per-case warm Qwen3.6 times were
23.54s (Requests), 19.62s (Gin), and 21.63s (Vue). Muse produced the same
deterministic quality result in 150.57s, 132.55s, and 110.47s respectively,
while making 3-6 retrieval calls and returning much longer answers. Muse's
default `reasoning_strength=high` also continued requesting a tool during
CodeNib's forced final-answer turn; setting the model's ATEM template kwarg to
`low` made the three-turn tool protocol complete successfully.

## Wiki benchmark

Both candidates used the same cached Requests outline, fresh isolated page
caches, and the same source-bound retrieval views.

| Candidate | Wall time | Model calls | Grounding | Citation coverage | Quality | Outcome |
|---|---:|---:|---:|---:|---:|---|
| DeepSeek V4 Flash | 12.58s | 1 | valid | 1.000 | valid | Keep as default |
| Muse Glimmer 30B Q4_K_M | 95.23s | 1 | valid | 1.000 | valid | 7.6x slower, no gate improvement |

DeepSeek is therefore protocol-compatible. The earlier broken pages were not
caused by a DeepSeek/OpenAI request mismatch. They came from stale or
unauthorized vector fallbacks, cold outline/page generation, and diagnostic
fact-plan prose that passed through to readers. The repaired path bounds model
calls, keeps invalid candidates out of the UI, retries degraded pages under an
operator-controlled budget, and deterministically removes repeated or thin
Overview sections.

## Index and page latency

All 26 manifests carry secure source fingerprint v2 identities and passed an
authenticated hybrid-view load plus a live dense query. Dense queries took
11-25ms after load. Cold full-view authorization/load time is dominated by
source recapture and artifact loading on large repositories:

| Repository | Cold view load |
|---|---:|
| Requests | 0.51s |
| bat | 0.60s |
| SymPy | 5.54s |
| Terraform | 6.09s |
| Ruff | 8.10s |
| MicroPython | 11.73s |
| Babel | 16.15s |

This separates the old bat 17-second page wait from index loading: bat's view
loads in about 0.6s, while an uncached remote Wiki generation has a much larger
long tail. After prewarming, all 26 Overview pages are current, with zero
missing, quality-invalid, fallback, scheduled-retry, or exhausted-retry
entries. A restarted backend served cached Overview pages in 3-4ms cold and
about 0.7ms warm; the bat source-linked graph took about 72ms. TOC construction
was 2-4ms after the one-time post-restart repository object initialization.

## Reproduction

```bash
make demo-ask-benchmark \
  DEMO_ASK_BENCHMARK_ARGS="--candidate qwen3.6-35b --timeout 300"

make demo-wiki-benchmark \
  DEMO_WIKI_BENCHMARK_ARGS="--config qa_config.local.yaml \
    --candidate deepseek-v4-flash --repo psf__requests"

make wiki-cache-prewarm \
  WIKI_CACHE_PREWARM_ARGS="--config qa_config.local.yaml --scope overview \
    --workers 2 --retry-degraded-now --fail-on-error \
    --fail-on-quality-invalid"

make wiki-cache-audit \
  WIKI_CACHE_AUDIT_ARGS="--config qa_config.local.yaml --require-overviews \
    --fail-on-fallback --fail-on-quality-invalid"
```

Benchmark JSON from this run was written under `/tmp/codenib/`; it is runtime
evidence rather than a tracked artifact because it contains host-specific
paths, endpoints, and timings.
