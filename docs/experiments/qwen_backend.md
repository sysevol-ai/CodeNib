<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Qwen2.5-Coder backend — agent compatibility + eval

Can the CodeMiner agent run on a **local open model** (Qwen2.5-Coder 7B / 14B /
32B via vLLM) instead of a cloud API, and does the pre-load context-engine still
help? Yes to running; pre-load helps, but its shape changes for weak models.

## How to run (local vLLM backend)

```bash
# serve (tool-calling ON; Qwen2.5 uses the Hermes <tool_call> format)
HF_HOME=/mnt/conda/huggingface python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-Coder-7B-Instruct --served-model-name qwen2.5-coder-7b \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len 32768 --max-num-seqs 64 --gpu-memory-utilization 0.85 --port 8001
# 32B: add `--quantization fp8`; lower --gpu-memory-utilization (~0.6) so the
# sweep's Qwen3-Embedding model still fits on the same GPU.

# run (openai/ + OPENAI_API_BASE routes litellm to the local server)
OPENAI_API_BASE=http://localhost:8001/v1 OPENAI_API_KEY=dummy \
PYTHONPATH=$PWD python scripts/agent_compile/run_sweep.py \
  --config scripts/agent_compile/configs/qwen7b_base.yaml \
  --model openai/qwen2.5-coder-7b --output-dir results/agent_compile/qwen7b_base
# synthesis dataset: same, via run_synthesis_sweep.py --model openai/... --synthesis-configs Python
```

## The tool-loop fix (why the first numbers were wrong)

Qwen2.5-Coder under the default `tool_choice="auto"` answered **one-shot**
(turns=1, tools=0) — it never grep/read, so the agent loop was a no-op and the
first sweep measured "the model guessing from memory," not an agent. The runner
only passed `tools=` and let tool_choice default to auto (fine for Claude, which
calls tools eagerly; dead for Qwen). Fix: `AgentRunner.first_turn_tool_choice`
forces a tool call on turn 0 (then auto), exposed via the config. After the fix
Qwen runs turns=2, tools≈1–2.4 — a real loop. **All numbers below are the fixed
(v2) run; the pre-fix run is discarded.**

## codeminer-base (files@k — base GT line spans are null, so file-level is the ruler)

100 instances/arm, paired, tool-loop fix on:

| model | grep_only f@5 | preinj_embed f@5 | Δ (preinj − grep) | mean tools |
|---|---|---|---|---|
| 7B  | 0.160 | 0.446 | **+0.286** | 1.1 |
| 14B | 0.310 | 0.594 | **+0.284** | 2.4 |
| 32B (fp8) | 0.362 | 0.545 | **+0.183** | 1.0 |

- **Pre-load helps every size** (+0.18 to +0.29 files@5).
- **`grep_only` rises monotonically with size** (0.16 → 0.31 → 0.36): bigger
  models use the tools better, so the bare agent is only respectable when large.
- **The pre-load lift shrinks as the model grows** (7B +0.286 → 32B +0.183):
  the stronger the model, the less it needs the injected starting point — i.e.
  **pre-load matters most for weak/local models**. (7B's grep is so poor it often
  greps the wrong file, so its grep_only is actually *worse* than guessing.)

## The compatibility findings that matter

- **Qwen2.5-Coder needs tool-calling forced on turn 0.** Under `auto` it answers
  one-shot; with `first_turn_tool_choice="required"` it genuinely loops
  (grep → read → answer). The Hermes parser/wiring was always correct — the
  model just won't *initiate* under auto. This is the single change that makes a
  local open model usable as an agent here.
- **Pre-load value is accuracy, and it's largest for weaker models.** Even with
  a real loop, the injected candidates give a poor explorer a correct starting
  point; the lift falls from +0.29 (7B) to +0.18 (32B) as the model gets better
  at finding code itself.
- **No local "save-token" axis.** litellm reports no `$` for a local endpoint,
  and the loops are short (turns≈2). On local models pre-load buys *accuracy*,
  not cost — the cost-saving story is a cloud / strong-agent property.

## Caveats / gotchas

- The GPU is **shared** — large models (32B fp8) OOM'd when other users' processes
  + the sweep's embedding model co-resided; tune `--gpu-memory-utilization` down
  (≈0.6 for 32B) and leave headroom for the embedding model (same GPU).
- vLLM startup: port conflicts, sampler-warmup OOM (`--max-num-seqs` down), and
  transient GPU-memory-release races between successive serves.
- reps=1; codeminer-base (files@k, GT spans null). A synthesis (per-category,
  span-overlap) re-run with the tool-loop fix is the natural next step — the
  pre-fix synthesis numbers were discarded with the rest of the no-loop run.
