<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Agent runtime probe — findings

**Question.** Beyond grep/read, does anything we add *inside the agent runtime*
earn its place — specifically (a) does compiled-context **pre-load** save tokens
at equal accuracy, (b) does **graph** expansion in the pre-load beat plain
embedding, (c) does a **verify-expand** closed loop (Layer 4) add value?

**TL;DR.** (a) **Yes** — embedding pre-load is equal-accuracy and **−17 % cost**,
significant. (b) **No** — graph in the pre-load is dominated by embedding. (c)
**No** — verify-expand is a no-op (0 / 400 triggers). The runtime's only proven
edge is the pre-load; the loop itself carries no demonstrated novelty. Product =
a flat **embedding pre-load context-engine** + the standard grep/read loop.

## Setup

- **Dataset:** `sysevol-ai/codeminer-synthesis` (per-query, per-category). 4
  languages — Go, Rust, TS/JS, C++/C. **Python excluded**: its sweep hung under
  5-way concurrency (rerun solo to add it). 1611 cells, reps = 1.
- **Model / scorer:** `vertex_ai/claude-haiku-4-5`; span-overlap localization
  (`answer_rec@5` = the agent's committed-answer recall). `cost_usd` is
  litellm's cache-aware price; output tokens dominate (~5× input).
- **Arms** (`configs/runtime_probe.yaml`), all on the same default tools
  (read/grep/glob/bash), differing only in pre-load:
  - `grep_only` — baseline (no pre-load).
  - `preinj_embed` — embedding candidates injected into the opening prompt.
  - `preinj_graph` — embedding **+ 1-hop call-graph expansion** injected.
  - `preinj_graph_verify` — graph pre-load **+ verify-expand** closed loop
    (`codenib.eval.agent_runner.verify_expand`): if the committed answer does
    not resolve to a real graph symbol, inject its 1-hop neighbours and answer
    once more.
- **Method:** per-query **paired bootstrap** (5000 resamples) of Δ vs
  `grep_only` (`pareto_ci.py`). Pre-load is a *variance trade* — it rescues
  queries grep fails on and distracts on others — so a point Δ at small n is
  noise (an n=10 behavioral "−0.20 regress" fully washed out by n=137). Verdict:
  **SAVE** = Δrec CI_lo ≥ −0.02 (equal accuracy) AND Δcost CI_hi < 0 (cheaper).

## Headline (pooled, n ≈ 400 / arm)

| arm (vs grep_only) | Δrec@5 [95% CI] | Δcost% [95% CI] | trigger | verdict |
|---|---|---|---|---|
| **preinj_embed** | **+0.018 [−0.013, +0.050]** | **−17.0 % [−20.4, −13.6]** | — | **SAVE** |
| preinj_graph | +0.016 [−0.020, +0.052] | −11.7 % [−14.7, −8.6] | — | inconclusive |
| preinj_graph_verify | +0.007 [−0.026, +0.039] | −9.6 % [−12.8, −6.1] | **0 / 400** | inconclusive (no-op) |

Equal accuracy across all three (CI straddles 0, point slightly positive); only
embedding clears the strict bar as a **SAVE**. The win mechanism is **fewer
turns** (≈ −1.8) — the agent confirms a pre-loaded candidate instead of
exploring — plus a stable, cache-friendly injected block.

## Per-category (absolute `answer_rec@5`; cost vs grep)

| category | grep | embed | graph | verify | embed Δcost |
|---|---|---|---|---|---|
| symbol_hint (grep-easy) | **0.904** | 0.879 | 0.854 | 0.854 | −17 % |
| module_hint | 0.803 | 0.844 | 0.853 | 0.819 | −20 % |
| reasoning | 0.792 | 0.829 | **0.867** | 0.879 | −18 % |
| file_hint | 0.732 | 0.765 | **0.814** | 0.740 | −11 % |
| behavioral | 0.740 | 0.750 | 0.731 | 0.750 | −21 % |
| traversal (grep-hard) | 0.630 | **0.641** | 0.605 | 0.582 | −16 % |

`contrib` (fraction of answer spans coming from a pre-injected candidate) was
~0.66–0.75 for embed — pre-load is genuinely used, not ignored.

## What the data decided

1. **SHIP: flat embedding pre-load.** Equal accuracy, −17 % cost, significant,
   prior-minimal (one recipe, no per-category routing). Cheaper on *every*
   category; accuracy up on module/reasoning/file, flat on behavioral/traversal,
   −0.025 on symbol_hint (where grep already wins by naming the symbol).
2. **CUT: graph in the pre-load.** Helps file_hint (0.732→0.814) and reasoning
   (0.792→0.867) but **hurts** symbol_hint (0.904→0.854) and — notably — loses
   on **traversal** (0.630→0.605), its supposed home turf. It also saves less
   than embedding (−12 % vs −17 %). Net: dominated. Keep the graph for on-demand
   navigation; don't blanket-inject it.
3. **CUT: verify-expand (Layer 4).** A **no-op**: 0 / 400 verify cells triggered
   the re-run. The resolution check never fires because the agent always cites a
   *real* graph symbol; its failure mode is the *wrong real* symbol, which a
   GT-free check cannot catch. `preinj_graph_verify ≈ preinj_graph`.

## Honest read on novelty

The **runtime loop has no demonstrated novelty** beyond pre-load, and pre-load
is "good code-specific RAG," not a novel loop mechanism — the compiler-precise
**graph did not beat plain embedding**. Novelty lives in the *compiler/index*
(multi-language SCIP graph, incremental) and in the *data/eval* (the synthesis
benchmark + span-overlap + this paired-bootstrap method), **not in the runtime
loop**. The only untested runtime-novelty candidate is completeness/impact
guarantees on genuinely *edge-shaped* tasks (the graph is its own ground truth
there) — but graph lost on traversal here, so the odds are modest and it needs a
different task (refactor-safety / impact), not this localization benchmark.

## Caveats

- Python missing (hung under concurrency; rerun solo to add the 5th language).
- reps = 1 (cross-query bootstrap captures variance; per-query rep noise not
  averaged). Per-category n = 40–144, so per-category CIs are wide — the
  **pooled** result is the significant one.
- verify no-op is mechanism-proven (0 % trigger), not merely a CI.

## Reproduce

```bash
# 3 pre-load arms (run per language; Python solo to avoid the concurrency hang)
PYTHONPATH=$PWD python scripts/agent_compile/run_synthesis_sweep.py \
  --config scripts/agent_compile/configs/runtime_probe.yaml \
  --output-dir results/agent_compile/runtime_probe --synthesis-configs Go
# verify arm (resume; only preinj_graph_verify runs)
#   ... same command after the 3-arm run completes for that language
python scripts/agent_compile/pareto_ci.py \
  --cells-dir results/agent_compile/runtime_probe/cells --boot 5000
python scripts/agent_compile/aggregate_synthesis.py \
  --cells-dir results/agent_compile/runtime_probe/cells --output-dir results/agent_compile/runtime_probe
```

Artifacts: cells + `pareto_ci.md` + `report_by_category.md` under
`${CODENIB_RESULTS_DIR}/runtime_probe_python/`. Design + decision:
`.claude/design/agent-runtime.md` §0.
