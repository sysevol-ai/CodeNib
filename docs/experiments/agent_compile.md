<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent-compile subset sweep — sample run (issue #133, Phase 2 dry-run)

> **⚠️ Pre-design-space results (2026-06).** The A0–A6 / `compile_table`
> numbers below are the historical methodology record from the original
> per-subset sweep. The experiment space is now the single canonical
> **`scripts/agent_compile/configs/design_space.yaml`** (9 arms, full split,
> neutral prompt), driven by the consolidated `run_sweep.py` +
> `aggregate.py` (reusable harness code lives under
> `codenib.eval.agent_runner`; the old `scripts/agent_compile/lib`
> compatibility namespace has been removed; the offline retrieval ablations moved to
> `scripts/retrieval_ablation/`). See
> [`scripts/agent_compile/README.md`](https://github.com/sysevol-ai/CodeMiner/blob/main/scripts/agent_compile/README.md).
> Fresh full-split design-space results will be added once the re-run completes.

This is the **sample** instantiation of the #133 Phase-2 experiment: run the
A0–A6 skill-subset matrix over a small multi-language slice of
`codenib_base`, record accuracy / cost / skill-usage per cell, and derive a
v1 `compile_table`. It is a *dry run on 5 instances* to validate the harness,
the metrics, and the analysis methodology end-to-end — **not** the full
instance-corpus result (open question §1 below).

Reproduce (canonical design-space sweep):

```bash
python scripts/agent_compile/run_sweep.py \
    --config scripts/agent_compile/configs/design_space.yaml \
    --output-dir results/agent_compile/design_space
python scripts/agent_compile/aggregate.py \
    --cells-dir results/agent_compile/design_space/cells \
    --output-dir results/agent_compile/design_space          # files@{1,3,5,10}
python scripts/agent_compile/aggregate.py \
    --cells-dir results/agent_compile/design_space/cells \
    --output-dir results/agent_compile/design_space_at1 \
    --metrics-k 1                                            # discriminating @1 view
```

`run_sweep.py` writes one JSON per cell under `<output-dir>/cells`, which is
exactly the directory `aggregate.py --cells-dir` consumes. `--metrics-k`
selects which `files@k` / `symbols@k` cutoffs the report folds (default
`1 3 5 10`); narrowing it to `1` gives the discriminating top-1 view used below.

## Setup

| | |
|---|---|
| Dataset | `fishmingyu/codeminer-base-dataset` (100 instances, 5 language groups) |
| Sample | 5 instances, 4 languages, both Python scenarios (see below) |
| Agent model | `vertex_ai/claude-haiku-4-5` @ `us-east5`, temp 0.0, max_turns 20 |
| Embeddings | `Qwen/Qwen3-Embedding-0.6B` (dim 1024), L2 chunks |
| Indexes | pre-built per-instance under `${CODENIB_PREBUILT_DIR}` — vector + symbol graph reused; BM25 built fresh from the prebuilt graph (see `codenib.eval.agent_runner.prebuilt`) |
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

The skill subsets A0–A6 are the #133 RFC table (the historical per-subset
config). `file_read` is always-on infrastructure and not a sweep variable.

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

The same table results when the floor is applied at `files@1`
(`aggregate.py --metrics-k 1`).

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
   supported here — flagged for re-test at the full-corpus scale.

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
   evaluation needs more than one instance per non-Python scenario.

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
  question §1 of #133 (run on the full instance corpus) is unchanged.
- **files@5 is saturated on the easy (py/ts) instances**, so the discriminating
  signal lives at files@1 and in tokens. The aggregator's `--metrics-k 1` view
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
`docs/experiments/agent_compile_runs/sample_defaults/`) confirms the headline results and
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
CodeNib's AoT (`compile_repo`) direction and is the recommended next
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

---

# Follow-up: an agent-friendly graph tool (`find_related_code`)

The cost study suggested `graph_expand` was ignored partly because it was
*unfriendly* (exact-name seeds, 8 knobs, code-body blobs). So we built a
LocAgent/CodeGraph-style replacement, `find_related_code(symbol, relation)`:
seed by a plain name (fuzzy-resolved; ambiguity returns candidates), `relation`
∈ {callers, callees, both}, and a **compact** result (name · file:line · kind ·
"caller/callee of X") with **no code bodies** — the agent uses `file_read` for
the one or two it cares about. Standalone it works well (bare `"doWatch"` →
40 compact related symbols, repo-relative paths).

**Result: still not adopted when grep coexists.** With `find_related_code`
available *and* the system prompt explicitly recommending it for caller/callee
and impact questions, neither haiku nor gemini-2.5-flash called it (0×) on
vuejs-11589 — both localized with `file_search` + `file_read`.

This reconciles our results with LocAgent: **LocAgent does not give the agent
grep/read — its graph tools are the only interface.** A well-formed graph tool
is *necessary but not sufficient* for adoption; when grep is available these
models prefer it on tasks grep can already solve. To realize the graph's value
we therefore need one of:

1. **Graph-only interface** (LocAgent-style): withhold file tools so graph
   navigation is the path. (Our `include_default_tools=false` enables this; the
   earlier "structured" run shows the agent then uses retrieval — though it
   spammed embedding rather than graph, so embedding may also need withholding
   to isolate graph.)
2. **Deterministic graph augmentation** (AoT): expand the call graph in the
   pipeline after the first hit, independent of the agent's tool choice.
3. **Task-targeted evaluation**: measure on impact / deep cross-file-chain
   localization where grep genuinely fails — not the moderate single-file
   localizations in this sample, where grep+read suffices.

`find_related_code` is the right *interface* (it matches the designs that work
in the literature); the open problem is **adoption / harness**, not the tool.

## Update: intent verbs (find_callers / find_callees / trace) — same outcome

Replaced the single `find_related_code` with three agent-native verbs:
`find_callers(symbol)`, `find_callees(symbol)`, `trace(from, to)` — sharing one
engine (`skills/_graphnav.py`, wrapping `CodeGraph.get_predecessors/successors`
+ shortest-path), compact (no bodies), fuzzy-resolved with ambiguity →
candidates. Functionally validated on vuejs (doWatch → 6 callers / 40 callees;
watchEffect()→doWatch() trace = 2 hops).

**Adoption unchanged: 0 graph-verb calls** on vuejs (haiku, verbs + grep both
available, prompt recommends them) — file_search×3, bm25×2, file_read×9.

We have now tried **three** graph-tool designs — generic `graph_expand`,
friendly `find_related_code`, and intent verbs — and **all three are ignored
whenever grep/read are available.** The tool shape is not the bottleneck; the
**harness** is. Per the CodeGraph analysis, adoption + token wins come from:
(a) serving the graph via the warm **MCP server** with host-agent steering
(CodeNib already has `codenib/mcp/server.py` + the manifest/compiler), and
(b) a **one-call context composer** (`codenib_context`) that runs
search→graph-expand→snippet-assembly *internally* so the agent makes one call
instead of choosing graph over grep — and (c) measuring at large-repo scale.
Building a nicer agent-chosen graph tool has been exhausted.

---

# The graph-aware harness works: `codenib_context` (one-call composer)

Since three *agent-chosen* graph tools were all ignored, we moved the graph
into the **harness**: `codenib_context(query)` — one call that internally
(1) searches for entry-point symbols (bm25 + embedding), (2) **deterministically
expands** each along the call graph (callers + callees via `_graphnav`), and
(3) returns a compact, deduped, budget-capped set (name · file:line · kind ·
relation; no bodies). The agent makes *one* call instead of choosing graph over
grep. The prompt steers "call codenib_context FIRST." (Loader change: `custom`
skills receive the full `contexts` dict so the composer can read both
`retrieve` and `expand`.)

## Result — adoption solved, and token savings where the agent fans out

haiku, defaults (grep/read) also available, vs the FREE grep/read baseline:

| instance | FREE files@5 / tokens | codenib_context files@5 / tokens | Δtokens | ctx calls |
|---|---|---|---|---|
| vuejs-11589 (cross-file) | 1.0 / 226 817 | 1.0 / **111 903** | **−51 %** | 1 |
| micropython-13569 | 1.0 / 140 828 | 1.0 / 137 666 | −2 % | 1 |
| babel-15445 (grep already cheap) | 1.0 / 117 202 | 1.0 / 131 969 | +13 % | 1 |
| **total** | 484 847 | **381 538** | **−21 %** | 3/3 |

Two findings:

1. **Adoption is solved by the harness, not the tool.** `codenib_context`
   was called in **3/3** cells — versus **0** for every standalone graph tool
   (graph_expand, find_related_code, find_callers/callees/trace). Framing the
   graph as a one-call "map this task" composer that the prompt tells the agent
   to call first gets it used; offering graph navigation as an optional tool
   next to grep does not.
2. **The token win shows where the grep agent fans out** — vuejs (cross-file)
   −51 % at equal accuracy; flat on micropython; slightly negative on babel,
   where grep already localized cheaply (the composer call isn't free). Net
   −21 % across three. This is exactly CodeGraph's profile: gains concentrate
   on large / cross-file cases and are marginal where grep was already
   efficient — so the headline win requires large-repo scale + more instances.

## Takeaway

This validates the project's value proposition on the right axis (**cost at
equal accuracy**) and via the right mechanism (a **graph-aware harness**, not a
free wrapper tool). Next: trust the composer more (the agent still did 6–7
confirming `file_read`s after the one context call — room for further savings),
benchmark at VS-Code / Next.js scale, and expose `codenib_context` +
`find_callers/callees/trace` over the MCP server (the infra already exists) for
the host-agent path CodeGraph uses.

## Large-repo check (sympy + matplotlib, codenib_base)

Picked larger Python repos from `codenib_base`: `sympy-13031`, `sympy-12419`
(~1.45k files), `matplotlib-14623` (~4.4k files, 3 GT files). FREE (grep/read)
vs `codenib_context`, haiku, reps=1:

| instance | FREE f@5 / tokens | codenib_context f@5 / tokens | Δtokens | ctx calls |
|---|---|---|---|---|
| sympy-12419 | 0.0 / 269 264 | 0.0 / **64 258** | **−76 %** | 1 |
| matplotlib-14623 | 0.0 / 240 370 | 0.0 / **89 098** | **−63 %** | 1 |
| sympy-13031 | 0.0 / 119 559 | 0.0 / 87 506 | −27 % | 1 |

**Two honest readings:**

1. **Token efficiency is real and grows with repo size** — the composer is
   adopted 3/3 and cuts tokens 27–76 % (largest on the biggest/most-fan-out
   cases), consistent with CodeGraph's "gains scale with size."
2. **But all three are unsolved by *both* conditions (f@5 = 0)** — so this batch
   shows "converges/fails cheaper," not an accuracy win. The clean accuracy+cost
   win remains the *solvable* cross-file case (vuejs: f@5 = 1.0 at −51 %).

Why the misses: e.g. sympy-13031's fix is in `SparseMatrix.hstack`
(`matrices/sparse.py`) — a **subclass override** — while both agents fixate on
the base `hstack` in `matrices/common.py`. Entry-point search points at the
base, and a 1-hop caller/callee expansion doesn't cross the inheritance/override
relationship to the subclass. So on hard cross-hierarchy localizations the
limiter is **entry-point search quality + missing inheritance/override edges in
the expansion**, not the harness mechanism. Both agents also hit the 12-turn cap
without converging.

**Implication:** the harness delivers the cost win on solvable tasks (and fails
cheaper on hard ones); raising accuracy on hard tasks needs (a) better
entry-point retrieval, (b) inheritance/override edges in `codenib_context`'s
expansion (not just call edges), and (c) better convergence so the agent trusts
the map instead of grepping to the turn cap.

---

# Headline: comparable accuracy at ~half the tokens (accuracy-first loop)

The cost win only counts if accuracy holds. We reframed the prompt as an
**accuracy-first EXPAND → READ → EXPAND loop**: `codenib_context` (search +
call-graph) is a *heuristic map* the agent calls first, but grep + file_read
stay fully in play and are how it *confirms* it reached the implementing file
(the metric credits any file the agent reads, so keeping read in the loop
protects accuracy). FREE (grep/read only) vs CONTEXT (grep/read + the composer),
6 instances, haiku, reps=1, max_turns=16:

| instance | FREE files@5 / tokens | CONTEXT files@5 / tokens | Δtokens |
|---|---|---|---|
| astropy-12907 | 1.0 / 306 817 | 1.0 / 174 651 | −43 % |
| axios-4731 | 1.0 / 256 181 | 1.0 / 73 788 | −71 % |
| vuejs-11589 | 1.0 / 254 330 | 1.0 / 214 139 | −16 % |
| caddy-5870 | 1.0 / 291 212 | 1.0 / 173 737 | −40 % |
| sympy-13031 | 0.0 / 343 737 | 0.0 / 133 166 | −61 % |
| matplotlib-14623 | 0.0 / 247 176 | 0.0 / 134 144 | −46 % |
| **total** | **1 699 453** | **903 625** | **−47 %** |

**Accuracy is identical on all 6** (4 solved by both at files@5 = 1.0; 2 hard
cross-hierarchy cases unsolved by both) — **zero accuracy regression** — at
**−47 % tokens** net (−16 % … −71 % per instance, every instance cheaper).

The loop is real: in the CONTEXT cells the agent calls `codenib_context` once,
then does 8–11 `file_read`s + 2–9 greps to confirm — `expand → read → expand`
with grep/read doing the reaching and the graph map focusing the search. This
is the project's value, demonstrated on the axis that matters: **match the pure
grep/read agent's accuracy with roughly half the tokens.**

Still open: the two hard cases (sympy-13031's subclass-override, matplotlib's
cross-file) remain unsolved by *both* conditions — parity, not regression. The
override-grep guidance didn't crack them; raising accuracy there is a separate
frontier (entry-point retrieval + inheritance/override edges in the expansion),
not a harness-vs-grep question.

---

# Diverse-repo broadening (Rust / TS / Go / C / Python) — what to improve

Ran FREE vs `codenib_context` on 6 previously-untested repos (haiku, reps=1,
max_turns=16). Accuracy-first read.

| instance (lang) | FREE files@5 / tokens | CONTEXT files@5 / tokens | Δtokens |
|---|---|---|---|
| tokio-4898 (Rust) | 1.0 / 94 965 | 1.0 / 81 721 | −14 % |
| docusaurus-10130 (TS) | 1.0 / 338 765 | 1.0 / 195 668 | −42 % |
| terraform-34814 (Go) | 1.0 / 338 294 | 1.0 / 194 550 | −42 % |
| xarray-2905 (Python) | 1.0 / 271 582 | 1.0 / 117 292 | −57 % |
| redis-10095 (C) | 1.0 / 97 524 | 1.0 / **258 158** | **+165 %** |
| bat-2201 (Rust) | 1.0 / — | — (GPU OOM) | — |

5 scored: **accuracy parity on all 5** (every cell 1.0 = 1.0, no regression);
net **−26 % tokens** — but dragged down by one regression.

## What to improve (ranked by the evidence)

1. **Composer must never net-harm (redis +165 %).** On redis the
   `codenib_context` call returned **0 results** (no error) — so the agent
   paid for the call, got nothing, and fanned out anyway (15 turns vs FREE's 9).
   Root cause: redis (C) symbols are stored as **content-hash node names**, so
   call-graph expansion off the bm25/embedding seeds yields nothing useful.
   Fixes: (a) always return the search seeds even when graph expansion is empty;
   (b) if the composed context is thin, *say so* so the agent doesn't double-pay;
   (c) cap/skip on low-value output.
2. **C/C++ graph quality.** Hash-named symbols + sparse C call edges
   (clangd/SCIP) make the graph far less useful than for Py/TS/Go/Rust — the
   composer's value is language-dependent. Needs readable C symbol names +
   better edges.
3. **Routing (this is the original agent-compile / CAR idea, full circle).** The
   composer helps on large/fan-out repos (docusaurus/terraform/xarray −42…−57 %)
   and *hurts* on small ones where grep is already cheap (redis FREE = 98 k).
   Decision: invoke `codenib_context` only when the scenario warrants it
   (repo size / expected fan-out), exactly the compile_table selection this RFC
   set out to build.
4. **Symbol-level accuracy.** Still measuring files@k; symbols@k remains the
   harder, more useful target.
5. **Serving / environment.** Vector load OOMs under GPU contention (bat) —
   a warm shared embedding server + memory management is needed for reliable
   runs and for the MCP serving path.
6. **Statistical hardening.** reps ≥ 3, larger instance set, and CIs before any
   headline number is quoted.

## Where it stands

Across all runs, the harness holds **accuracy parity with the grep/read agent**
and saves tokens on medium/large/fan-out repos (the −16…−71 % cases), with two
known failure modes: (i) the composer returning empty (C / redis) and net-harming,
and (ii) hard cross-hierarchy localizations unsolved by both. The next, highest-
leverage work is **routing** (#3 — only invoke when it pays) and **composer
robustness** (#1), which together turn the −47 % demo into a dependable win.

> **⚠️ Correction (see next section).** Every `codenib_context` cell above —
> including the "−47 %" headline and the "−26 % / redis +165 %" diverse run —
> ran with a composer that returned **0 results in every cell** due to a context-
> packaging bug. Those token deltas measured a *no-op* tool call vs the grep/read
> agent (pure variance), and the "weak C/C++ graph" diagnosis was wrong. The bug
> and the first valid comparison are below.

---

# Correction + real result: the composer was a no-op; root cause and fix

**The bug.** `codenib_context` is `skill_type: custom`. `build_skill_contexts`
correctly loaded bm25 + vector + symbol_graph (its declared
`index_requirements`), but `_package_contexts` only wrapped them into the
`retrieve`/`expand` context objects for `RETRIEVAL`/`AGGREGATE`/`EXPAND` skill
*types*. A lone `custom` composer matched neither, so the loaded indexes were
**silently dropped** → the executor received `retrieve=None, expand=None` and
returned `[]`. Confirmed across **every** recorded CTX cell: `n_results: 0`
(acc_ctx and div_ctx alike). The graph-aware harness had never actually run.

**The "weak C/C++ graph" was a presentation bug, not a quality problem.** redis
has the real names all along: 8860/9068 nodes are content-hash `name`d (clang/
SCIP identity keys) but **every one carries a readable `unified_name`** — e.g.
`src/t_list.c:popGenericCommand()`. The composer and `_graphnav` surfaced only
the hash, which is ungroundable. Nothing was wrong with the edges.

**Fixes.**
1. `_package_contexts` now builds `retrieve`/`expand` whenever the matching
   index artifacts *loaded*, not just by skill type — so a custom composer that
   declares the requirements gets wired. (regression test:
   `test_custom_composer_alone_gets_retrieve_and_expand`)
2. `_graphnav` + the composer surface `unified_name` for display **and accept it
   for re-seeding** (`resolve()` maps a readable bare/qualified name back to the
   canonical key for `get_successors`/`predecessors`). Zero cost to languages
   whose `name` is already readable; makes C/C++ groundable. (regression tests:
   `test_*_for_hash_graph`, `test_resolve_bare_symbol_to_hash_canonical`)

Verified on redis post-fix: composer **0 → 14 readable, on-topic symbols**
(`RESP2_NULL_BULK_STRING`, `RESP2_NULL_ARRAY`, `parseBulk()` — exactly the RESP
null-reply path the LPOP fix edits).

## First valid FREE vs `codenib_context` (haiku, reps=1, max_turns=16, CPU embed)

| instance (lang) | FREE f@1 / f@5 / tok | CTX f@1 / f@5 / tok | n_results | Δtok |
|---|---|---|---|---|
| docusaurus (TS) | 1.0 / 1.0 / 193 919 | 1.0 / 1.0 / 231 484 | 15 | +19 % |
| terraform (Go) | 1.0 / 1.0 / 353 055 | **0.0** / 1.0 / 135 271 | 16 | −62 % |
| xarray (Py) | **0.0** / 1.0 / 327 741 | **1.0** / 1.0 / 142 135 | 23 | −57 % |
| redis (C++) | 1.0 / 1.0 / 161 009 | 1.0 / 1.0 / 208 809 | 19 | +30 % |
| tokio (Rust) | 1.0 / 1.0 / 103 460 | 1.0 / 1.0 / 70 422 | 25 | −32 % |

- **files@5 parity on all 5** (1.0 = 1.0). At @1, one swing each way
  (terraform CTX 1.0→0.0, xarray CTX 0.0→1.0) — within single-rep noise.
- Net **−31 % tokens** this run. The **redis C++ regression collapsed from +165 %
  to +30 %** — the no-op composer was the cause, not the C graph.
- **Magnitude is not yet trustworthy.** redis FREE swung **97 k → 161 k (+65 %)
  on the identical cell config** between two runs; tokio FREE was stable
  (95 k → 103 k). Single-rep token deltas are directional only — **reps ≥ 3 + CIs
  required** before quoting any headline number.

## What this changes about "what to improve"

- The composer now genuinely returns graph-aware context everywhere, including
  C/C++. "Composer never net-harm" is largely addressed (no more empty returns
  on the wired path); the residual is **token efficiency, not correctness**.
- **Routing still matters, now for tokens not bugs:** redis/docusaurus go
  token-positive (grep is already cheap on small/well-named-file repos), while
  terraform/xarray/tokio save 32–62 %. Gate the composer on expected fan-out
  (repo size / hits) — the original CAR `compile_table` selection.
- Next: reps ≥ 3 with CIs on this set to pin the real distribution, then the
  routing gate.

---

# Definitive 3-way ablation: does the LSP graph earn its keep? (reps=3, N=8)

The CTX-vs-GREP comparisons above conflate two things — the composer is
**search seeds (bm25+embedding) + call-graph expansion** — and earlier runs had
graph expansion partly broken (see the two bugs below). To fairly isolate the
graph we run three arms that share everything (same haiku, temp=0, max_turns=16,
same 8 repos/queries/indexes, reps=3) and differ in exactly one variable:

| arm | search seeds | graph expansion | isolates |
|---|---|---|---|
| **A** grep/read only (`file_read`+`file_search`) | — | — | baseline agent |
| **B** composer, `CODENIB_COMPOSER_NO_GRAPH=1` | ✓ | — | value of *search* |
| **C** composer, full | ✓ | ✓ | value of *graph* |

Accuracy reduced as MEAN over reps; tokens as MEDIAN over reps (robust to the
±60–115 % single-rep swing); deltas paired per instance, Student-t 95 % CI.

**A second graph-LSP bug, found and fixed first.** The prebuilt graph ships an
EMPTY `_unified_to_names` even though every vertex has a `unified_name`, so the
readable seeds the embedding search returns (`popGenericCommand`, …) never
resolved to a vertex → the on-target embedding seeds were NEVER expanded; only
hash-named bm25 seeds were. Fixed by rebuilding the index from vertex
attributes. Post-fix the composer expands both sources (median 15 vs search
only's 5 nodes). Arm C below uses the fixed graph.

## Results

| metric | A grep | B search-only | C +graph |
|---|---|---|---|
| files@1 (mean) | 0.58 | **0.79** | 0.75 |
| files@5 (mean) | 0.88 | 0.88 | 0.88 |
| symbols@5 (mean) | 0.00 | **0.25** | 0.25 |
| tokens vs A (paired median) | — | **−19 %** [CI −34, −3] | −7 % [CI −23, +8] |

- **B − A (search):** **−19 % tokens, 95 % CI excludes 0 — a real win** at equal
  files@5, and it *lifts* accuracy (files@1 0.58→0.79; symbols@5 0→0.25).
- **C − B (graph, the fair test):** **+18 % tokens** [CI −12, +48], **no accuracy
  gain** at any k (slightly worse at files@1, incl. an xarray regression). Graph
  helps on only 2/8 (redis −21 %, terraform −3 %) and adds cost on 6/8.

## Conclusion (honest)

**The retrieval earns its keep; the LSP call-graph expansion, fairly measured,
does not — on this localization@k benchmark.** The token win and the accuracy
lift both come from the bm25+embedding seeds. Adding graph expansion costs
~18 % more tokens (more context the agent reads but doesn't convert into fewer
turns) for zero accuracy gain.

Why — and what it does *not* mean: the graph isn't broken (edges are real —
redis has 86 k reference edges; `lpopCommand→redisCommandTable`; redis itself
benefits −21 % when expansion lands on the edit-site neighborhood). The issue is
**headroom**: search already puts the file in top-5 (0.88) / top-1 (0.79) on
these mostly single-file SWE-bench fixes, so multi-hop call-graph traversal has
little to add and only adds cost. The graph's hypothesized value — reaching
files/symbols search *misses* via cross-file causal tracing — is not exercised
by this task distribution.

# LocAgent-style graph-primary harness (bm25 + call-graph, NO grep)

Tests the natural objection: maybe graph only saves tokens once you remove the
grep escape hatch (the LocAgent regime — graph as the navigation spine). Arm:
`file_read` only (no `file_search`), tools = bm25 + find_callers / find_callees /
trace / impact_analysis. reps=3, N=8, vs the grep/read baseline (`cost_grep`).

| instance (lang) | GREP f@5 / tok | LocAgent f@5 / tok | Δtok |
|---|---|---|---|
| tokio (Rust) | 1.0 / 110 996 | 1.0 / 50 969 | −54 % |
| redis (C++) | 1.0 / 136 202 | 1.0 / 143 720 | +6 % |
| terraform (Go) | 1.0 / 204 862 | 1.0 / 270 039 | +32 % |
| docusaurus (TS) | 1.0 / 213 827 | 1.0 / 379 622 | +78 % |
| axios (TS) | 1.0 / 127 673 | 1.0 / 246 011 | +93 % |
| caddy (Go) | 1.0 / 127 103 | 1.0 / 317 414 | +150 % |
| sympy (Py) | 0.0 / 118 694 | **1.0** / 430 115 | +262 % |
| xarray (Py) | 1.0 / 123 925 | 1.0 / 545 293 | +340 % |

**Two findings, both decisive:**

1. **Zero graph-tool adoption.** Across all 24 cells the agent called
   `bm25_search` (7.6/cell) + `file_read` (4.9/cell) and the call-graph verbs
   (`find_callers`/`find_callees`/`trace`/`impact_analysis`) **0 times** — even
   though grep was removed and the graph was the *only* structured navigation
   tool. The model reaches for the primitives it was pretrained on (search +
   read), not bespoke graph tools, regardless of what's on offer.
2. **Removing grep costs +113 % tokens [95 % CI +2 %, +224 %]**, not less.
   Without grep the agent fans out with bm25 + read, a worse substitute. files@5
   held (and sympy went 0→1.0 — but via bm25+read, *not* the graph: 0 graph
   calls).

## Strict LocAgent: ban file_read too (read code ONLY by symbol)

The purest graph-primary regime — `include_default_tools: false` (no grep, no
file_read); the agent reads code only via `read_code_block(symbol)` (resolve →
node span). reps=3, N=8.

| metric | result |
|---|---|
| tool use | bm25_search 8.8/cell, read_code_block 3.2/cell, **graph verbs ~0.25/cell (6 calls / 24)** |
| tokens vs grep/read | **+77 % [95 % CI +6 %, +148 %]** |
| accuracy | files@5 **regressed** on docusaurus (1.0→0.67); rest parity, sympy still 0 |

Removing the filesystem entirely is *worse*: still essentially no graph
navigation, +77 % tokens, and an accuracy regression (read-by-symbol gives only
the node span, so the agent can't browse surrounding context). Across **four**
harness designs now — additive composer, free-choice verbs, graph-primary
(no grep, +113 %), strict (no filesystem, +77 %) — the call-graph tools are used
0–0.25×/cell and removing filesystem primitives only raises cost (and can hurt
accuracy). The agent's preferred primitives are search + read, full stop.

## Faithful LocAgent: bm25 returns NAME TAGS only (the proper setup)

The arms above had a flaw: `bm25_search` returned full code bodies
(`return_content: true`), which both ballooned tokens (the bodies re-accumulate
in the re-sent transcript) AND handed the agent the code directly, so it never
needed to traverse. The faithful fix (`bm25_names`: name + file:line, NO bodies)
forces navigation by name and read-by-symbol. reps=3, N=8.

| metric | content-bm25 LocAgent | **faithful (names-only)** |
|---|---|---|
| graph-tool use | 0 / 24 cells | **18 / 24 cells** (≥1 call) |
| graph verbs / cell | 0 | ~1.6 (vs bm25_names 10.9 + read_code_block 6.3) |
| tokens vs grep/read | +113 % | **+43 % [95 % CI +4 %, +83 %]** |
| accuracy | files@5 parity | **regresses 3/8** (axios, docusaurus 1.0→0.0; redis 1.0→0.67) |

**The fix worked as predicted and sharpens — not overturns — the conclusion.**
Names-only bm25 (a) halved the token penalty (the bodies *were* the bloat) and
(b) finally triggered graph use (18/24 cells). But the harness is still **worse
than grep/read on both axes**: +43 % tokens *and* accuracy regressions, because
reading a bare symbol span (no grep, no surrounding-file context) makes the
agent miss. And even with names-only, graph navigation is a small minority of
actions (~1.6/cell vs ~17 search+read) — the agent reads the *named* candidate
directly rather than traversing to it, since for localization search already
names the target.

**Six harness designs** (vs the grep/read agent). The **"graph used"** column is
critical: several arms *offered* the graph tools but the agent barely touched
them, so they are NOT tests of the graph — read them as "search+read without
grep."

| harness | graph used (cells / total calls) | tokens | accuracy | tests the graph? |
|---|---|---|---|---|
| search-only composer (+grep/read) | — | **−19 %** | parity+ | no (search seeds) |
| additive composer (+graph, +grep/read) | deterministic, 1×/cell | +18 % | parity | **yes (forced)** |
| LocAgent (content-bm25, no grep) | **0/24, 0 calls** | +113 % | parity | **NO — graph unused** |
| strict (no fs, read_code_block) | 5/24, 6 calls | +77 % | −1 regression | barely |
| faithful (names-bm25, no fs) | **18/24, 39 calls** | +43 % | −3 regressions | **yes (agent-chosen)** |
| **everything (grep+read+search+graph all available)** | **1/24, 1 call** | +9 % [CI −19,+36] | −1 regression | **NO — graph unused despite being offered** |

The **everything-available** arm (task #16) is the decisive one for *voluntary*
adoption: give the agent grep + file_read + bm25_names + the graph verbs +
read_code_block all at once, with a neutral prompt that pushes nothing. Result:
the agent localizes with file_read (5.8/cell) + grep (5.4) + bm25_names (2.2) +
read_code_block (1.9) and calls the call-graph **once in 24 cells**. So the
faithful arm's 18/24 graph use was purely an artifact of *removing grep*; restore
grep and voluntary graph use collapses to ~0. Tokens are neutral (+9 %, CI spans
0) because grep is back — far cheaper than the no-grep arms.

So only **two** rows actually exercise the call-graph:

1. **additive composer** — the graph is expanded *deterministically* (not by
   agent choice); the ablation C−B isolates it at **+18 % tokens, no accuracy
   gain** at any k.
2. **faithful LocAgent** — the agent *chooses* to traverse (18/24 cells, 39
   calls), at **+43 % tokens and 3 accuracy regressions**.

The `content-bm25 LocAgent` (+113 %) and `strict` (+77 %) rows are **not** graph
results — the agent used ~0 graph calls; they only show that removing grep and
leaning on bm25+read is expensive. The honest conclusion stands on the rows that
*do* test the graph: whether forced (deterministic, +18 %/no gain) or chosen
(faithful, +43 %/−3 acc), **the call-graph does not help in the agent loop on
localization.** And the **everything-available** arm settles the adoption
question outright: when the agent has grep *and* the graph, it picks the graph
**1 time in 24 cells** — so the graph is not its tool of choice for localization,
period. The only config that beats grep/read is **search seeds *added to*
grep/read** (−19 %). The graph's value is offline — the dependency-analysis
plugin.

**The LocAgent token-savings thesis does not replicate here.** Likely because
LocAgent uses a graph-*native* agent (trained/forced to navigate the graph) vs
a weak baseline; our zero-shot model, given strong search + read, ignores the
graph interface and grep is simply an efficient localization primitive that the
graph-primary harness removes. Consistent across all three harness designs we
measured: prefetch composer (additive overhead), free-choice graph verbs (~0
adoption), and graph-primary/no-grep (0 adoption + +113 % tokens). For this
model on localization, **search + grep/read is the efficient frontier; the
call-graph does not earn its keep in the agent loop** — only offline, as the
dependency-analysis plugin.

**Decisions this supports:**
1. **Ship the retrieval composer with graph expansion OFF by default** for
   localization (the proven −19 % arm), exposing graph as opt-in.
2. If graph stays on, **gate it** to the redis-like case (seeds cluster in a
   call neighborhood near the edit site) — routing, but now *inside* the
   composer, not composer-vs-grep.
3. To prove the graph's value at all, **evaluate on cross-file tasks where
   search alone fails** (the regime call-graphs are for); localization@k is the
   wrong instrument.

---

# Finding the regime where the graph DOES help (dataset-wide recall scan)

Rather than oversell the graph as a universal token-saver (it isn't — see
above), find the scenarios where it genuinely adds value. The honest, isolating
question, asked cheaply over **all 100 instances** with no agent/LLM
(`graph_recall_ablation.py`):

> Does the composer surface a target file that **deep search misses** — i.e. a
> target reachable only by a caller/callee edge from a search hit, not by
> retrieving more search results? (The composer's seeds are a subset of deep
> search, so any recovered target *must* come from graph expansion.)

Budget K=30 (bm25 top-30 ∪ embedding top-30) vs composer (5 seeds + graph).

| language | graph recovers a search-miss | search misses ≥1 target | total |
|---|---|---|---|
| C++/C | 0 | 4 | 20 |
| Go | 1 | 5 | 21 |
| Python | 2 | 4 | 20 |
| Rust | 2 | 8 | 20 |
| TS/JS | 0 | 3 | 19 |
| **all** | **5** | **24** | **100** |

**The graph-favorable regime is real but niche: ~5 % of instances (≈21 % of the
24 cases where search misses).** On 76/100, deep search already has every target
file — no headroom for the graph at file-recall level. The 5 recoveries:

- `sympy-13031` → `sympy/matrices/sparse.py`
- `matplotlib-14623` → `lib/mpl_toolkits/mplot3d/axes3d.py`
- `terraform-35611` → `internal/terraform/transform_attach_config_resource.go`
- `ruff-15309`, `ruff-15356` (Rust) → linter rule files

Notably `sympy-13031` is exactly the instance the grep/read agent failed
end-to-end (files@5 = 0.00) — search can't reach `sparse.py`, but it's a
callee/caller hop from a search hit. (End-to-end confirmation on these 5 —
search-only vs graph — runs next.)

**Takeaway for a comprehensive system:** the call-graph isn't the default
localization win; it's a candidate **rescue path for the ~5 % of bugs whose edit
site is one call-edge away from what search finds** but unreachable by search
alone. Whether that retrieval-recall gain converts end-to-end is tested next.

## End-to-end on the 5 favorable instances: recall ≠ accuracy

Running search-only (graph OFF) vs +graph (reps=3) on exactly the 5 instances
where the graph recovers a search-missed target file:

| instance (lang) | search-only f@5 | +graph f@5 | Δtok |
|---|---|---|---|
| ruff-15309 (Rust) | 0.33 | **1.00** | +12 % |
| sympy-13031 (Py) | 0.00 | **0.33** | +81 % |
| matplotlib-14623 (Py) | 0.00 | 0.00 | +18 % |
| ruff-15356 (Rust) | 0.67 | **0.00** | +0 % |
| terraform-35611 (Go) | 1.00 | **0.00** | +16 % |

**Mixed: 2 helped, 2 hurt, 1 unchanged; +25 % tokens.** The retrieval-level
recovery does **not** reliably convert to agent accuracy. Worse, the extra graph
context can **distract** the agent into a worse localization than search alone
(terraform: search-only nails it at 1.0; +graph drops to 0.0). N=5 is noisy, but
the direction is clear and consistent with the dataset-wide ablation: **adding
graph context to a strong search+grep agent is not a reliable win — even in the
regime where the graph demonstrably improves retrieval recall.** More retrieved
context is not more localized truth; distractors cost both tokens and accuracy.

## Overall verdict (honest, not oversold)

1. **Retrieval (bm25+embedding) is the proven win:** −19 % tokens at
   equal-or-better accuracy, CI excludes zero; lifts files@1 0.58→0.79.
2. **The LSP call-graph expansion does not earn its keep on localization@k** —
   net token cost, no reliable accuracy gain, and it can distract. It helps only
   sporadically (ruff-15309, sympy) and hurts about as often.
3. **Ship the retrieval composer with graph expansion OFF by default.** Keep the
   graph as opt-in and as the indexing/relationship backbone of the system; its
   end-to-end value needs (a) a different task regime (multi-file, cross-file
   causal tracing where search alone fails *and* the agent can't grep around it)
   and/or (b) a graph-primary harness that *substitutes* graph hops for grep/read
   fan-out rather than adding to it — the additive setup measured here shows the
   agent double-dips (1 composer call but still 5 greps + 7 reads/cell), so graph
   context is read-once overhead, not a replacement.

---

# GraphRAG as a *retriever* (recall@k, not the agent loop) — #24

The graph fails in the agent loop but the composer (search seeds → call-graph
expansion) is a legitimate **retrieval pipeline**. Evaluated as a retriever
(`scripts/agent_compile/graphrag_retrieve.py`, files@k recall vs ground truth,
no LLM) over all **100 codenib-base** instances:

| recall@k | search-only | GraphRAG (search+graph) | GraphRAG + identifier seeding |
|---|---|---|---|
| files@1 | 10 % | 10 % | **23 %** |
| files@5 | 47 % | 48 % | 49 % |
| files@10 | 47 % | **52 %** | 52 % |

Two distinct, honest wins:

1. **Graph expansion is a strictly-additive recall booster: files@10 47 %→52 %
   (+5 instances, ZERO regressions).** It appends caller/callee neighbors to the
   search seeds, so it can only add the right file, never drop it — the opposite
   of its net-negative behavior as an agent tool. The lift sits at @10 (not @5)
   because neighbors land in ranks 6–30; the budget squeezes them out of top-5.

2. **Identifier seeding doubles top-1 precision: files@1 10 %→23 %.** This is a
   *seeding-stage* improvement (independent of graph expansion): extract the
   code symbols the user named in the query (backtick-quoted, camelCase,
   ALL_CAPS commands like `LPOP`, snake_case; minus bug-report noise) and
   bm25-search each as a leading interleaved seed source. Fixes the redis-class
   miss where NL prose ("null array") misleads full-text bm25 but the named
   command (`LPOP`→`lpopCommand`) seeds from the right place. Its gain is
   concentrated at @1; @5/@10 show minor churn (+4/−3, +5/−5) as identifier hits
   reshuffle the budget — net @5 +1, @10 ±0.

**Takeaway:** the call-graph and query-identifier signals both help *retrieval*
(graph: +5 @10 additive; identifiers: 2× @1) even though neither helps the agent
loop. This is the graph's real home — a recall-boosting retrieval pipeline, run
from scripts, not an in-loop agent tool. (Caveat: identifier seeding would also
lift a search-only baseline — it's a seeding gain, not a graph gain; the graph's
own contribution is the strictly-additive +5 @10.)

---

# Fair index comparison: flat vs IVF vs graph-scoped (#25)

Our vector store is `IndexFlatIP` (exhaustive). To judge the graph's retrieval
value fairly you must control for the index — an IVF/ANN index speeds up flat
search with NO graph. `index_compare.py`, recall@k + query latency over 100
codenib-base instances (no LLM):

| method | median ms | p90 ms | files@1 | files@5 | files@10 |
|---|---|---|---|---|---|
| flat (exact) | 1.22 | 3.82 | 34 | 49 | 56 |
| IVF (approx) | **0.29** | **0.65** | 33 | 49 | 55 |
| graph-scoped (PPR) | 7.20 | 29.48 | **26** | **33** | **36** |

1. **IVF: ~4× faster than flat at identical recall** (33/49/55 ≈ 34/49/56). The
   speed comes from the ANN index, not the graph — so "the graph makes retrieval
   faster" is unsupported; a plain IVF index gets it with zero graph.
2. **Graph-scoping is strictly worse on both axes**: 6× slower than flat (PPR
   compute) AND much lower recall (26/33/36). Restricting candidates to the
   call-graph subgraph *excludes relevant files* — refutes "scope the query to a
   subgraph helps" for this variant. (Seeds resolved on 77/100; the other 23 had
   no graph-resolvable seed → empty scoped result, part of the recall drop.)

**Caveat — what was tested:** the scoped arm RANKS the PPR subgraph (graph-only
ranking from flat-top-5 seeds), it does NOT do embedding search *restricted to*
the subgraph's vectors (the literal "limit the query in a subgraph" idea). That
faithful variant is untested — it needs a subgraph-node → vector-row map
(fragile under C/C++ hash-vs-readable naming). But since PPR-scoping already
loses recall by excluding relevant files, embedding-within-subgraph faces the
same risk: the target file must be inside the chosen subgraph.

**Verdict on GraphRAG's retrieval value:** the only positive is the *additive*
expansion (+5 files@10, zero regressions; earlier section). Graph does NOT win
speed (IVF does) and does NOT win via scoping (hurts). For retrieval at this
scale, **IVF is the win** (4× faster, same recall); the call-graph's real value
remains offline structural/impact analysis (the dependency_subgraph tool), not
faster or more-accurate flat retrieval.
