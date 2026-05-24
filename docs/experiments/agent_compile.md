<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Agent-compile subset sweep — sample run (issue #133, Phase 2 dry-run)

This is the **sample** instantiation of the #133 Phase-2 experiment: run the
A0–A6 skill-subset matrix over a small multi-language slice of
`codeminer_base`, record accuracy / cost / skill-usage per cell, and derive a
v1 `compile_table`. It is a *dry run on 5 instances* to validate the harness,
the metrics, and the analysis methodology end-to-end — **not** the full
30-instance fit-pool result (open question §1 below).

Reproduce:

```bash
python scripts/agent_compile/run_sample_sweep.py \
    --config configs/agent_compile/sample.yaml \
    --output-dir results/agent_compile/sample
python scripts/agent_compile/aggregate_phase2.py \
    --cells-dir results/agent_compile/sample/cells \
    --output-dir results/agent_compile/sample          # files@5 (RFC standard)
python scripts/agent_compile/aggregate_phase2.py \
    --cells-dir results/agent_compile/sample/cells \
    --output-dir results/agent_compile/sample_at1 --target-k 1   # discriminating view
```

## Setup

| | |
|---|---|
| Dataset | `fishmingyu/codeminer-base-dataset` (100 instances, 5 language groups) |
| Sample | 5 instances, 4 languages, both Python scenarios (see below) |
| Agent model | `vertex_ai/claude-haiku-4-5` @ `us-east5`, temp 0.0, max_turns 20 |
| Embeddings | `Qwen/Qwen3-Embedding-0.6B` (dim 1024), L2 chunks |
| Indexes | pre-built per-instance under `/mnt/data/codeminer` — vector + symbol graph reused; BM25 built fresh from the prebuilt graph (see `scripts/agent_compile/prebuilt.py`) |
| Reps | 2 — cost = min across reps, accuracy = mean across reps (per #131) |
| Metric | `files@k` (LocAgent / PR #128 column set); `symbols@k` also recorded |

Sample instances (one per scenario cell that has prebuilt indexes):

| instance | language | has_stacktrace | scenario key |
|---|---|---|---|
| `astropy__astropy-12907` | Python | no | `python:no_stacktrace` |
| `scikit-learn__scikit-learn-13142` | Python | yes | `python:stacktrace` |
| `caddyserver__caddy-5870` | Go | no | `go:no_stacktrace` |
| `axios__axios-4731` | TS/JS | no | `typescript:no_stacktrace` |
| `astral-sh__ruff-15309` | Rust | no | `rust:no_stacktrace` |

The skill subsets A0–A6 are the #133 RFC table (`configs/agent_compile/sample.yaml`).
`file_read` is always-on infrastructure and not a sweep variable.

## Per-subset results (mean over 5 instances)

| subset | skills | tokens | turns | cost $ | files@1 | files@5 | f@5 easy | f@5 **hard** | eligible |
|---|---|---|---|---|---|---|---|---|---|
| A0 | bm25 | 93 245 | 4.4 | 0.098 | 0.20 | 0.60 | 1.00 | **0.00** | yes (baseline) |
| **A1** | **embedding** | **83 209** | 4.4 | **0.088** | **0.60** | **0.80** | 1.00 | **0.50** | **yes** |
| A2 | bm25+embedding | 88 215 | 4.6 | 0.093 | 0.20 | 0.60 | 1.00 | 0.00 | NO |
| A3 | bm25+graph | 93 300 | 4.8 | 0.099 | 0.20 | 0.60 | 1.00 | 0.00 | NO |
| A4 | bm25+embedding+graph | 99 851 | 5.0 | 0.105 | 0.20 | 0.60 | 1.00 | 0.00 | NO |
| A5 | A4+regex | 120 293 | 5.6 | 0.126 | 0.20 | 0.60 | 1.00 | 0.00 | NO |
| A6 | full registry (9) | 93 194 | 4.6 | 0.098 | 0.20 | 0.60 | 1.00 | 0.00 | NO |

**Pareto front (files@5 ↑ / tokens ↓): {A1}.** A1 (embedding-only) *dominates*
the full registry A6 — strictly cheaper **and** strictly more accurate.

## Derived `compile_table` (files@5 floor, τ = A6 − 0.05)

| scenario | chosen subset | files@5 | skills |
|---|---|---|---|
| `python:no_stacktrace` | A0 | 1.00 | bm25_search |
| `python:stacktrace` | A1 | 1.00 | embedding_search |
| `go:no_stacktrace` | A1 | 1.00 | embedding_search |
| `typescript:no_stacktrace` | A1 | 1.00 | embedding_search |
| `rust:no_stacktrace` | A0 | 0.00 | bm25_search (fallback — see §rust) |

The same table results when the floor is applied at `files@1` (`--target-k 1`).

## Findings (read these critically — small N)

1. **Embedding-only (A1) is the robust default and Pareto-dominant.** It beats
   the full registry on accuracy *and* cost. The marginal value of piling on
   skills is zero-to-negative on this sample.

2. **BM25 can actively mislead the agent.** On the hard Go instance (`caddy`),
   A0 scored files@5 = 0 and *thrashed* (18 BM25 calls, 475 k tokens). Worse,
   subsets that contain **both** BM25 and embedding (A2/A4/A6) still scored
   files@5 = 0 there — the agent preferentially calls BM25, is misled, and
   never falls back to the embedding tool it was given. This is a tool-choice
   pathology, not an index quality problem: A1 (embedding only) solves the same
   instance in 3 calls / 42 k tokens.

3. **`graph_expand` showed no benefit.** A3 (bm25+graph) matched A0's accuracy
   at equal-or-higher token cost (and up to 233 k tokens on one rep). It was
   invoked in only 20–50 % of cells and never produced an accuracy lift. The
   hypothesis that graph expansion improves token efficiency is **not**
   supported here — flagged for re-test at the full fit-pool scale.

4. **The agent ignores most of the full registry.** In A6, five of nine skills
   (`hybrid_search`, `embedding_rerank`, `llm_rerank`, `query_transform`,
   `code_to_query`) had invocation rate < 5 %. This is the core motivation for
   agent-compile: the registry already behaves like a much smaller set.

5. **Saturation guard did its job.** `caddy` and `ruff` are the "hard"
   instances (BM25 alone misses at files@5). Only A1 has lift on the hard slice
   (0.50 vs A0's 0.00); every BM25-led subset's apparent value lives entirely
   in the easy slice and is therefore marked **ineligible** for `compile_table`.
   This is why `python:no_stacktrace` selects A0 over the *cheaper* A3 — A3 is
   globally ineligible.

6. **`scikit-learn` (stacktrace): embedding ranks the file #1, BM25 doesn't.**
   files@5 is saturated (both = 1.0) but files@1 is 1.0 for A1 and 0.0 for the
   BM25-led subsets — concrete support for the `has_stacktrace` dimension.

7. **Rust (`ruff`) is unsolved by every subset** (files@5 = 0 everywhere). The
   table entry is an honest cheapest-fallback, not a recommendation. A real
   fit pool needs more than one instance per non-Python scenario.

8. **Rep variance is large** (e.g. A4 on `caddy`: 124 k vs 171 k tokens; A0:
   475 k vs 193 k). The min-across-reps cost estimator (#131) is doing real
   work; single-rep numbers would be misleading.

## `symbols@k`

`symbols@k` is recorded but stays low even where files@k = 1.0 — e.g.
`astropy-12907`'s GT symbol is the helper `_cstack()` while the agent surfaces
the more salient `separability_matrix` / `_calculate_separability_matrix`.
File-level hit, symbol-level miss: the metric is trustworthy, the task is just
harder at symbol granularity.

## Limitations / open questions

- **N = 5, one instance per non-Python scenario.** The per-scenario
  `compile_table` cells are illustrative, not statistically grounded. Open
  question §1 of #133 (run on the 30-instance fit pool) is unchanged.
- **files@5 is saturated on the easy (py/ts) instances**, so the discriminating
  signal lives at files@1 and in tokens. The aggregator's `--target-k 1` view
  is provided for that reason.
- **One embedding model, one agent model.** Cross-model variance (ADR
  model-matrix) is out of scope for this sample.
- Selection used `τ = A6 files@k − 0.05` (RFC open question §2); an absolute
  floor would change the rust fallback only.
