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

---

# Follow-up: default tool layer (file_read + file_search)

The sweep above ran **without** filesystem primitives — the agent could only
call retrieval skills. That is unrealistic (real coding agents have
bash/grep/read) and it hid a methodology bug. This follow-up adds an always-on
default tool layer (`file_read` + `file_search`: grep / glob / shell, from
#145 / PR #169) under every A0–A6 subset and re-runs.

**Setup delta:** defaults always-on; `graph_expand` made seedable from a file
(`seed_files=[...]`) so the agent can pivot from a file it read into the call
graph; system prompt rewritten as an explore→locate→expand→read→answer
workflow + `<environment>` block; **localization scored from the agent's final
answer + the files it `file_read` + retrieval-skill nodes** (not skill nodes
alone); `max_turns` 20→10. Re-run: reps=1 on 3 shared instances
(`astropy-12907` py, `caddy-5870` go, `axios-4731` ts).

## Baseline (no defaults) vs default tool layer — same 3 instances

| subset | base files@5 | base tokens | **+defaults** files@5 | **+defaults** tokens | graph_expand cells (base→def) |
|---|---|---|---|---|---|
| A0 bm25 | 0.67 | 128 k | **1.00** | 134 k | 0 → 0 |
| A1 embedding | 1.00 | 113 k | 1.00 | **84 k** | 0 → 0 |
| A2 bm25+emb | 0.67 | 111 k | **1.00** | 133 k | 0 → 0 |
| A3 bm25+graph | 0.67 | 107 k | **1.00** | 159 k | **3 → 0** |
| A4 trio | 0.67 | 124 k | **1.00** | 152 k | 2 → 0 |
| A5 trio+regex | 0.67 | 152 k | **1.00** | 160 k | 1 → 0 |
| A6 full | 0.67 | 110 k | **1.00** | 183 k | **3 → 0** |

Pareto front (files@5 ↑ / tokens ↓) with defaults: **{A1}** (embedding +
file layer — cheapest at full accuracy).

## Findings

1. **The default layer makes accuracy uniform.** files@5 goes 0.67 → **1.00
   for every subset**: file navigation rescues the instances bm25/embedding
   alone missed. Once the agent can grep/read, the *retrieval skill choice
   stops mattering for accuracy* on this sample — the only remaining
   differentiator is token cost.

2. **The model abandons `graph_expand` when given file tools — even when it's
   trivial to seed.** In the no-defaults baseline the agent *did* call
   `graph_expand` (3/3 cells in A3 and A6). With the default layer it calls it
   **0 times across all 21 cells**, despite (a) `seed_files=[...]` requiring
   only a path it already read and (b) the system prompt explicitly preferring
   it over grepping. `file_read`/`file_search` are invoked in **100 %** of
   cells. The file layer *cannibalized* graph navigation. This is consistent
   with OpenHands' observation that LLMs strongly prefer bash/grep/read over
   bespoke tools — and it means graph/LSP value will not surface from
   voluntary tool choice with this model; it must be made the path of least
   resistance (or forced) to be measured.

3. **More skills = more cost, no accuracy gain.** Beyond A1, every added skill
   only raises tokens (A1 84 k → A6 183 k, 2.2×) at identical files@5 and
   equal-or-worse files@1. With a default file layer, the bespoke retrieval
   stack does not earn its keep on this sample.

4. **Scoring had to change.** Scoring localization from retrieval-skill node
   outputs alone scored 0 for an agent that reads files (verified: on caddy
   the agent read the GT `admin.go` ~10× yet scored 0 and ran to the turn
   cap). Localization is now scored from the agent's answer (`Files:` /
   `Symbols:` lines) + `file_read` targets + skill nodes.

5. **Convergence.** The default-tool agent over-explored: at `max_turns=20` it
   hit the cap 50–100 % of the time (3–6× tokens). `max_turns=10` ~halves cost
   with no accuracy loss, but the agent still uses all 10 turns — it is
   truncated, not self-converging. A firmer stop signal is future work.

## Implications for agent-compile

- With a competent default tool layer, the A0–A6 *accuracy* sweep collapses
  (everything hits files@5 = 1.0 on this sample); the meaningful axis becomes
  **token cost**, where embedding-only (A1) dominates and graph/extra skills
  are pure overhead.
- The open research question shifts from "which skills for which scenario"
  to **"can graph/LSP navigation be made the agent's preferred move so its
  token-efficiency advantage is realized?"** — e.g. expose graph_expand as the
  primary "find related code" verb, or evaluate a model less biased toward
  grep. Testing the LSP-saves-tokens thesis likely requires *withholding*
  `file_search` (forcing graph use) as a controlled condition.

## Corroboration at larger N (5 instances, reps=2, max_turns=20)

A fuller default-tool run over all 5 instances × 2 reps (70 cells,
`results/agent_compile/sample_defaults/`) confirms the headline results and
strengthens two of them:

- **`graph_expand` invocation = 0 % across all 70 cells** — the agent never
  reaches for it once file tools exist, regardless of subset, instance, or
  rep. Not a fluke of the 3-instance run.
- **The file layer solved the previously-unsolvable instance.** `ruff-15309`
  (Rust) scored files@5 = 0 for *every* subset in the no-defaults sweep; with
  the default layer the **hard set is empty** — all 5 instances become "easy"
  (files@5 0.90–1.00 across subsets). File navigation generalizes where the
  language-specific retrieval stack did not.
- Cost at `max_turns=20` is 200–311 k tokens/subset (vs 84–183 k at
  `max_turns=10`), re-confirming the over-exploration the tighter budget curbs.

# Cost study: can structured navigation beat the grep/read agent?

The previous sections showed the file-tool agent reaches high accuracy but at
high token cost, and never uses `graph_expand`. This study tests the hoped-for
payoff directly: **withhold the file tools (force embedding + graph) and see if
the agent reaches the same files at lower cost.** FREE = file tools +
bm25/embedding; STRUCTURED = `include_default_tools=false`, only
`embedding_search` + `graph_expand`. 3 hard/big-repo instances, reps=1,
`max_turns=12`, two models (haiku-4.5, gemini-2.5-flash).

| model | instance | FREE files@5 / tokens | STRUCTURED files@5 / tokens | Δtokens |
|---|---|---|---|---|
| haiku | babel-15445 | 1.0 / 117 k | 1.0 / 135 k | +15 % |
| haiku | vuejs-11589 | 1.0 / 227 k | 1.0 / 196 k | −13 % |
| haiku | micropython-13569 | 1.0 / 141 k | 1.0 / **424 k** | **+201 %** |
| gemini | babel-15445 | **0.0** / 69 k | 1.0 / 30 k | (free failed) |
| gemini | vuejs-11589 | 1.0 / 129 k | **0.0** / 58 k | (struct failed) |
| gemini | micropython-13569 | **0.0** / 134 k | 1.0 / 72 k | (free failed) |

## Findings — the cost-saving thesis did NOT cleanly replicate

1. **Withholding file tools does not force graph use — it forces
   *embedding-spam*.** Across all 6 structured cells the agent called
   `embedding_search` 8–13× and `graph_expand` **once total** (and that one
   errored). Given grep it greps; given no grep it re-queries embedding. It
   never adopts graph navigation. So "structured" here really means
   "embedding-only", not "graph/LSP".

2. **That can be *more* expensive, not less.** micropython structured =
   424 k tokens (13 embedding calls, each returning large chunks) vs 141 k for
   the free grep/read agent — a 3× regression. A promising single-instance
   smoke (vuejs −13 %) did not generalize; haiku structured was **+56 %
   tokens overall**.

3. **gemini-2.5-flash is cheaper but flaky** (free 1/3, structured 2/3 at
   files@5=1.0; 30–134 k tokens). When it *does* solve via the structured path
   it is very cheap (babel 30 k), hinting the efficient-structured regime
   exists — but accuracy variance at reps=1 makes per-instance comparison
   unreliable.

## Conclusion — graph value needs a *deterministic* path, not agent tool choice

The agent will not choose `graph_expand` under any condition (file tools on:
greps; file tools off: embedding-spams). Therefore the graph/LSP
token-efficiency advantage **cannot be demonstrated through free agent tool
selection** with these models. To realize it, graph expansion must be a
**deterministic pipeline step** — e.g. after the first retrieval hit,
automatically expand along the call graph and feed the result to the agent —
rather than an optional tool the agent is trusted to call. This matches
CodeMiner's AoT (`compile_repo`) direction and is the recommended next
experiment. A fair test also needs reps ≥ 3 (tool-calling variance is large)
and a graph-only condition (embedding withheld too) to isolate graph from
embedding.

## Caveats

- Headline table: 3 instances, reps=1, one agent model (vertex haiku-4.5);
  corroboration: 5 instances, reps=2. Directional, not statistically grounded.
  Baseline numbers are restricted to the 3 shared instances from the (reps=2,
  max_turns=20) run above, so token magnitudes are not perfectly matched
  across conditions.
- `graph_expand`'s `seed_files` path is exercised by unit tests but, because
  the agent never called it, has no end-to-end agent coverage here.
