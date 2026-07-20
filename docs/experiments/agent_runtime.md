<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Agent runtime probe — findings

## Cross-family extension protocol (2026-07-20)

The publication matrix now includes two independent replications:

- **Gemma 4-12B-it**, served locally through the OpenAI-compatible vLLM API,
  adds an open-weight model family near the existing Qwen3.5-9B scale.
- **Gemini 2.5 Flash**, served by Vertex AI, adds a second closed provider.

Both use the same 500 synthesis queries, three arms, top-10 frozen L2
candidates, 16-turn cap, 48,000-token history budget, 4,096-token completion
budget, and temperature zero as the completed Qwen runs. Thinking is disabled:
Gemma uses the non-thinking chat-template path, while CodeMiner sends Gemini
2.5 `thinkingBudget=0`. Results remain paired and normalized within model;
absolute token counts are not compared across tokenizers or providers.

Gemma is served from the immutable Hub revision recorded in the run manifest.
Its vLLM process must use `--tool-call-parser gemma4`,
`--reasoning-parser gemma4`, and the Gemma 4 tool chat template. Before a full
matrix is admitted, each provider must pass a function-call smoke and a
stratified pilot with complete query-arm keys, no executed unadvertised tools,
and no systematic context or answer-contract failure.

The local Gemma server reserves 60% of the H100 for model and KV state, leaving
capacity for the frozen Qwen3-Embedding-0.6B query encoder and its retained CUDA
allocator state across 25 repository indexes.
It uses one serving sequence, a 131,072-token serving cap, and vLLM's
generation defaults; these launch controls and the chat-template digest are
persisted in the result summary.

The 100-query pilot takes four queries from each of the 25 repositories with
`--query-selection category_round_robin`. The category order rotates by
repository, preventing the dataset's category-blocked row order from turning
the pilot into an all-`behavioral` sample. Full runs consume all 500 queries and
therefore do not apply a query cap or selection strategy.

## Full five-model replication (updated 2026-07-20)

The original four-language probe below is now historical. The publication
matrix repeats the three-arm experiment across five model variants from four
families on one frozen, balanced workload:

- **Dataset:** all 500 queries from `sysevol-ai/codeminer-synthesis`, covering
  25 repository snapshots and five language groups (100 queries each).
- **Arms:** `grep_only`, `preinj_eager`, and `preinj_eager_compact`; all expose
  exactly `read`, `grep`, `glob`, and `bash` to the model. Both preload arms use
  the same top-10 L2 embedding context. Compact mode performs a one-time
  explore-to-commit collapse after the first successful read. The formal recipes
  retain that newest read, the read paths, and a bounded latest assessment; they
  drop the preload and earlier exploration transcript without another model call.
- **Models:** Claude Haiku 4.5, Qwen3.5-9B/27B, Gemma 4-12B, and Gemini 2.5
  Flash. Comparisons are paired and normalized within each model; absolute
  token counts are not compared across providers or model sizes.
- **Primary metric:** total prompt-plus-completion tokens over the agent
  trajectory. The quality guardrail is the paired change in committed-answer
  block Recall@5, with a uniform operational threshold of -0.05. This is a
  reporting guardrail, not a pre-registered equivalence or non-inferiority
  test.
- **Cache boundary:** token totals are logical provider-reported usage. Prefix
  caching can reuse unchanged prefixes but does not remove retained KV state;
  the experiment does not claim cache-hit, GPU-memory, latency, or throughput
  improvements.
- **Inference:** 10,000-sample percentile bootstrap clustered by repository
  snapshot. Language strata contain five repository clusters each, so the
  pooled result is the headline result.

| model / arm | tokens vs. grep [95% CI] | delta block R@5 [95% CI] | guardrail |
|---|---:|---:|---|
| Haiku / eager | 49.9% [44.2, 56.7] | -0.009 [-0.043, +0.022] | pass |
| Haiku / compact | 61.6% [55.0, 68.1] | +0.009 [-0.021, +0.041] | pass |
| Qwen3.5-9B / eager | 51.5% [46.3, 57.1] | +0.017 [-0.027, +0.071] | pass |
| **Qwen3.5-9B / compact** | **45.1% [40.2, 50.1]** | **-0.006 [-0.047, +0.038]** | **pass** |
| Qwen3.5-27B / eager | 57.0% [52.2, 61.9] | -0.011 [-0.055, +0.040] | fail (borderline) |
| **Qwen3.5-27B / compact** | **44.8% [40.3, 49.9]** | **-0.003 [-0.037, +0.038]** | **pass** |
| Gemma 4-12B / eager | 46.2% [37.6, 55.2] | +0.110 [+0.059, +0.161] | pass |
| **Gemma 4-12B / compact** | **12.9% [10.2, 16.1]** | **+0.040 [-0.012, +0.093]** | **pass** |
| Gemini 2.5 Flash / eager | 46.9% [37.6, 58.3] | +0.111 [+0.063, +0.165] | pass |
| **Gemini 2.5 Flash / compact** | **35.8% [27.7, 46.6]** | **+0.067 [+0.025, +0.111]** | **pass** |

For compact model-level reporting, we select the lowest-token compiled-context
arm whose recall-delta 95% interval stays above -0.05. This rule selects eager
for Haiku and compact for the other four models; the selected arms use
**12.9-49.9% of baseline tokens**. The mechanism is model-dependent: compact
costs 123.3% of eager tokens for Haiku but only 27.9% for Gemma. Gemma and
Gemini compact also lose 0.070 and 0.044 Recall@5 relative to eager, while
remaining above the baseline quality guard because eager improves over
baseline. We therefore do not promote one history policy unconditionally; the
deployed model must clear the same quality gate.

Each Qwen matrix contains 1,500 successful cells (500 per arm), with no missing
or duplicate query-arm keys. The 9B run had two initial request failures; both
failed records are preserved and both cells succeeded under an unchanged,
serial retry. It also produced 97 answer-contract failures, which remain in the
quality analysis as zero Block Recall rather than being filtered out. Eighteen
tool attempts returned errors, including two attempts to call an unadvertised
`answer` tool; no unadvertised tool call executed. The 27B run had one initial
malformed tool-call response, preserved and successfully retried under the
unchanged protocol.

Each new matrix also contains 1,500 successful, meaningful cells. Gemma has 39
answer-format failures and 30 rejected tool-call errors; Gemini has 28 and 11.
Malformed or unadvertised tool attempts are retained in the audit, and neither
model executes an unadvertised tool. Gemini's full matrix costs $8.65 at the
recorded Vertex rates. All invalid answers remain in the quality analysis as
zero recall.

Inside the frozen paper artifact bundle, the inputs are under
`inputs/agent/`, the publication metrics are
`verification/expected/agent_runtime.json`, and the expected raster outputs are
`verification/expected/agent_runtime.png` and
`verification/expected/agent_runtime_breakdown.png`. The bundle-level
`paper_artifact_config.json` and `figure/reproduce_paper_figures.py` reconstruct
both figures. The paper panel compresses the pooled language and query-type
forests beside the model-level results; `agent_runtime_breakdown.png` preserves
the expanded 2-by-2 audit view. `SOURCE_LOCK.json` records the exact CodeMiner
and figure-source commits. The metrics JSON fingerprints the 1,500 selected
cells per model and the available run and protocol manifests.

The sections below document the earlier runtime-probe question, graph and
verify-expand ablations, and their original four-language result.

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
    (`codeminer.eval.agent_runner.verify_expand`): if the committed answer does
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

The cross-family runs use the frozen configs above. Fetch
[`tool_chat_template_gemma4.jinja`](https://raw.githubusercontent.com/vllm-project/vllm/v0.25.1/examples/tool_chat_template_gemma4.jinja)
from the vLLM `v0.25.1` tag and verify the SHA-256 digest recorded in
`gemma4_12b_synth_runtime.yaml` before launching the local server:

```bash
vllm serve google/gemma-4-12B-it \
  --revision 12ace6d648d72bd41519e140f1185f34d38c7e3d \
  --served-model-name gemma4-12b --host 127.0.0.1 --port 8001 \
  --dtype bfloat16 --max-model-len 131072 --max-num-seqs 1 \
  --gpu-memory-utilization 0.60 --language-model-only \
  --enable-auto-tool-choice --tool-call-parser gemma4 \
  --reasoning-parser gemma4 --chat-template "$GEMMA4_CHAT_TEMPLATE" \
  --default-chat-template-kwargs '{"enable_thinking": false}' \
  --enable-prefix-caching --generation-config vllm --disable-log-stats

# Use --max-queries 4 --query-selection category_round_robin for the pilot.
OPENAI_API_BASE=http://127.0.0.1:8001/v1 OPENAI_API_KEY=dummy \
python scripts/agent_compile/run_synthesis_sweep.py \
  --config scripts/agent_compile/configs/gemma4_12b_synth_runtime.yaml \
  --output-dir /mnt/data/codeminer/results/gemma4_12b_synth_runtime_v1 \
  --synthesis-configs Python,Go,Rust,TypeScript_JavaScript,C++_C

VERTEXAI_PROJECT="$VERTEXAI_PROJECT" VERTEXAI_LOCATION=global \
python scripts/agent_compile/run_synthesis_sweep.py \
  --config scripts/agent_compile/configs/gemini25_flash_synth_runtime.yaml \
  --output-dir /mnt/data/codeminer/results/gemini25_flash_synth_runtime_v1 \
  --synthesis-configs Python,Go,Rust,TypeScript_JavaScript,C++_C
```

Run local and remote-provider matrices serially on a single-GPU host. The query
runner retains CUDA allocator state while switching repository indexes, so an
idle vLLM process can otherwise cause late context-load OOMs in the remote run.

The commands below reproduce the historical runtime probe rather than the
cross-family extension:

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
`/mnt/data/codeminer/results/runtime_probe_python/`. Design + decision:
`.claude/design/agent-runtime.md` §0.
