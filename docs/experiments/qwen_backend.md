<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Local open-model backend (Qwen2.5-Coder + Qwen3.5) — agent eval

Can the CodeMiner agent run on **local open models** instead of a cloud API, and
does the pre-load context-engine help? Yes to running. The headline finding:

> **Pre-load's value is inversely proportional to the model's own agentic
> ability.** Weak models that can't explore are rescued by it; strong agents that
> explore well get equal accuracy and only a cost saving — and only in the
> many-query setting where index reuse is real.

Two model families, on the same agent + prebuilt indexes:
- **Qwen2.5-Coder** 7B/14B/32B (vLLM `--tool-call-parser hermes`)
- **Qwen3.5** 4B/9B/27B (vLLM 0.23, `--tool-call-parser qwen3_xml`)

For the native-LSP Base study, the open-model secondary block uses
`openai/qwen3.5-27b`, matching the strongest completed Base agent run. It must
use a separate result root and `--secondary-model --disable-thinking`; the
pinned Haiku primary manifest and its confirmatory inference remain unchanged.

## How to run

```bash
# Qwen2.5-Coder (vLLM >=0.10)
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-Coder-7B-Instruct --served-model-name qwen2.5-coder-7b \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len 32768 --max-num-seqs 64 --gpu-memory-utilization 0.85 --port 8001

# Qwen3.5 (needs vLLM 0.23 in a separate env: torch 2.11/cu13; driver CUDA 13 OK)
export QWEN_RUNTIME_ROOT=/mnt/data/codeminer
export TMPDIR=${QWEN_RUNTIME_ROOT}/tmp/vllm
export VLLM_CACHE_ROOT=${QWEN_RUNTIME_ROOT}/cache/vllm
export TORCHINDUCTOR_CACHE_DIR=${QWEN_RUNTIME_ROOT}/cache/torchinductor
export TRITON_CACHE_DIR=${QWEN_RUNTIME_ROOT}/cache/triton
export CUDA_CACHE_PATH=${QWEN_RUNTIME_ROOT}/cache/cuda
mkdir -p "$TMPDIR" "$VLLM_CACHE_ROOT" "$TORCHINDUCTOR_CACHE_DIR" \
  "$TRITON_CACHE_DIR" "$CUDA_CACHE_PATH"
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3.5-4B --served-model-name qwen3.5-4b \
  --enable-auto-tool-choice --tool-call-parser qwen3_xml \
  --max-model-len 32768 --gpu-memory-utilization 0.45 --port 8001

# Native-LSP secondary block (H100 80 GB)
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3.5-27B --served-model-name qwen3.5-27b \
  --enable-auto-tool-choice --tool-call-parser qwen3_xml \
  --max-model-len 65536 --max-num-seqs 4 \
  --gpu-memory-utilization 0.90 --port 8001

# run (openai/ + OPENAI_API_BASE routes litellm to the local server)
OPENAI_API_BASE=http://localhost:8001/v1 OPENAI_API_KEY=dummy PYTHONPATH=$PWD \
python scripts/agent_compile/run_sweep.py \
  --config scripts/agent_compile/configs/qwen35_base.yaml \
  --model openai/qwen3.5-4b --output-dir results/agent_compile/qwen35_4b
# synthesis (many-query): run_synthesis_sweep.py --model openai/... --synthesis-configs Python
```

`qwen7b_base.yaml` sets `first_turn_tool_choice: required` (Qwen2.5 needs it,
see below); `qwen35_base.yaml` leaves it null (Qwen3.5 tool-calls under auto).

## codeminer-base — single query / repo (files@5 + span@5)

`span@5` = `answer_blocks@5`, span-overlap recall (see methodology). Paired,
100 inst/arm, `format_fail=0%` for every cell below.

**Qwen2.5-Coder** (forced first-turn tool call; otherwise answers one-shot):

| model | grep files@5 | preinj files@5 | grep span@5 | preinj span@5 |
|---|---|---|---|---|
| 7B  | 0.160 | **0.446** | 0.130 | **0.277** |
| 14B | 0.310 | **0.594** | 0.162 | **0.381** |
| 32B | 0.362 | **0.545** | 0.194 | **0.343** |

→ Pre-load **helps massively at every size** (files +0.18-0.29, span +0.15-0.22).
These models barely explore (turns~2 even when forced), so the injected
candidates *are* the localization.

**Qwen3.5** (native agent: turns 12-15, read-rate 97-100%, no forcing):

| model | grep files@5 | preinj files@5 | grep span@5 | preinj span@5 |
|---|---|---|---|---|
| 4B  | 0.730 | 0.656 | 0.402 | 0.281 |
| 9B (partial) | 0.770 | 0.768 | 0.552 | 0.402 |
| 27B | 0.890 | 0.881 | 0.618 | 0.524 |

→ Pre-load is **equal accuracy or slightly negative**. 27B paired bootstrap:
files Δ=−0.013 CI=[−0.125,+0.100], span Δ=−0.083 CI=[−0.250,+0.087] — **both
CIs span 0 → equal**. The strong agent already explores to the answer; injected
candidates add no accuracy (and can mislead — see synthesis). 9B is partial
(n≈52; the run was repurposed mid-way for the max-turns probe).

**The cross-family story is the whole point:** Qwen2.5 (turns~2, can't explore)
is rescued by pre-load; Qwen3.5 (turns~14, explores natively) is not.

## codeminer-synthesis — many queries / repo (where reuse is real)

base is 1 query/instance, so the index build amortizes over nothing and pre-load
can't show its reuse value. synthesis (50-80 q/repo) is the right setting.
**Qwen3.5-27B**, span answer_rec, paired bootstrap (`pareto_ci.py`):

| axis | preinj_embed vs grep_only |
|---|---|
| accuracy (ALL) | Δrec@5 = −0.056 [−0.127, +0.014] → equal |
| turns | **−1.7** |
| tokens | **−7.6 %** |
| behavioral category | Δrec@5 = **−0.167 [−0.333, −0.042] → REGRESS** |

→ Now pre-load **saves cost** (−1.7 turns, −7.6 % tokens) at equal pooled
accuracy — the saving base structurally couldn't show. **But it's a trade, not a
free win:** on `behavioral` (no-hint, explore-only queries) the agent
over-trusts candidates (fewer turns, wrong answer) — a significant regress. Cf.
Haiku on synthesis (+0.018 / −17 %): same class (equal-accuracy + save), but
Haiku saves more (cloud prompt-cache) and doesn't regress on behavioral.

## Methodology

**Span-overlap, not symbol@k.** Symbol-level localization is reported as
span-overlap recall (`answer_blocks@k`). Exact identifier match (`symbol@k`) is
**fundamentally ill-suited to agents**: an agent emits free-form prose
(`Driver::new`, `dir_entry_dict()`, backtick'd names), not canonical symbol IDs,
so string-matching GT identifiers scores ≈0 regardless of whether it located the
code (measured: symbol@k = 0.000 across all Qwen3.5 base cells). Span-overlap
aligns on code **position** — the common ground between a generative agent and
ground truth — capturing "located the symbol's code" robustly, at slightly
coarser granularity (credits overlapping/enclosing symbols).

**Honest format-failure reporting.** A cell scores 0 for two different reasons:
wrong localization, or the agent found the code but never emitted a parseable
`Files:/Symbols:/Locations:` answer (an LLM format-following weakness, worst on
small models). `run_sweep` tags the latter `format_failed`; `aggregate_honest.py`
reports the failure rate separately from accuracy instead of silently zeroing it.
The harness retries (force-schema, reads-hint) to give the model a fair chance,
then discloses the residual rate.

## Harness fixes this surfaced (all in this PR)

1. **`first_turn_tool_choice`** — Qwen2.5 answers one-shot under `tool_choice=auto`
   (turns=1, tools=0, loop is a no-op). Force a tool call on turn 0. *Default
   None = unchanged for Claude.*
2. **force-until-read** — first-turn forcing wasn't enough: 32B fired one grep,
   got 0 hits, blind-answered from candidates (4% read vs 86% for 14B). Force
   tools until a file is actually read.
3. **last-turn salvage + force-schema retries** — weak models burn all turns
   narrating; on the last turn force a tool-free schema answer, retry to the full
   3-line contract, hint the files actually read.
4. **format_failed flag + aggregate_honest.py** — the honesty reporting above.

## Next step

Pre-load on a strong agent is a *trade* (saves cost, regresses behavioral)
because candidates and the generic grep/read loop were never fused. The
direction is a **pre-load-aware harness** that triages candidates (rule-out /
verify / fallback-to-explore) instead of consuming them linearly. Design:
[`.claude/design/preload-aware-harness.md`](https://github.com/sysevol-ai/CodeMiner/blob/main/.claude/design/preload-aware-harness.md).

## Caveats / gotchas

- vLLM 0.23 for Qwen3.5 needs torch 2.11/cu13 — clone the env, don't upgrade in
  place. Driver CUDA 13 already supports it; no system change needed.
- Qwen3.5's first H100 startup JIT-compiles FlashInfer GDN kernels. NVCC uses
  `TMPDIR`; point it and the vLLM/Torch/Triton caches at a large data volume or
  the parallel compile can fill a small root filesystem before the API opens.
- Use a 65,536-token server context for the 27B native-LSP secondary block. A
  32,768-token pilot reached the harness's final structured-answer request with
  28,673 input tokens plus a 4,096-token output allowance and received HTTP
  400. The otherwise identical 65,536-token pilot completed all three arms.
- Qwen3.5 uses the `qwen3_xml` tool-call format (`<function=…><parameter=…>`),
  **not** hermes — wrong parser silently drops all tool calls under auto.
- GPU is **shared**: leave headroom for the sweep's Qwen3-Embedding on the same
  GPU (32B fp8 needs `--gpu-memory-utilization ≈0.6`).
- Env landmine: a SWE-bench test repo was editable-installed as `sympy`,
  shadowing real sympy and failing whole sweeps — `pip install "sympy>=1.13"`.
- reps=1. 9B base is partial. Per-category synthesis n is small (CIs wide); the
  pooled / cross-family results are the robust ones.
- Raising max_turns 16->40 (probe): grep_only gains (more exploration helps a
  strong agent), preinj does not (it already converges early) — consistent with
  the equal-accuracy finding.
