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

## codeminer-base (files@k — base GT line spans are null, so file-level is the ruler)

| model | grep_only f@1 | preinj_embed f@1 | grep_only f@5 | preinj_embed f@5 |
|---|---|---|---|---|
| 7B  | 0.190 | 0.345 | 0.222 | 0.357 |
| 14B | 0.286 | 0.357 | 0.343 | 0.514 |
| 32B (fp8) | 0.139 | **0.388** | 0.191 | **0.555** |

- **Pre-load helps every size** — on 32B it lifts files@5 0.191 → 0.555.
- **Bigger is better** with pre-load (f@5 0.357 → 0.514 → 0.555).
- `grep_only` is noisy across sizes (not perfectly paired n); `preinj_embed` is
  the clean, monotonic axis.

## codeminer-synthesis (Qwen 7B, answer_rec@5 span-overlap, per category)

| category | Δrec@5 (preinj − grep) [95% CI] | win/tie/loss |
|---|---|---|
| behavioral | +0.057 [0.00, +0.14] | 2/33/0 |
| traversal  | +0.078 [0.00, +0.17] | 3/12/0 |
| file_hint  | −0.133 [−0.40, +0.07] (n=15) | 1/11/3 |
| module_hint / symbol_hint / reasoning | ≈ 0.000 | all ties |
| **ALL** | **+0.012 [−0.045, +0.070]** | 7/89/4 |

Mirrors the Claude pattern (pre-load helps the no-hint categories
behavioral/traversal, ties on hint-rich symbol/module/file), weaker + noisier.

## The compatibility finding that matters

- **Qwen2.5-Coder-Instruct does not initiate tool calls under `tool_choice: auto`**
  (what the agent loop uses). With `required` it emits a correct Hermes
  `<tool_call>`, so the parser/wiring is fine — the model simply *chooses* not to
  grep/read. Every cell ran in **turns=1, tools=0**: a one-shot answer.
- Consequence: for a weak/local model the agent loop is effectively dead, and
  **pre-load is what makes it usable** — on base it ~doubles file accuracy
  because the injected candidates carry the answer the model won't go find.
- **There is no "save token" axis locally**: litellm reports no `$` cost for the
  local endpoint, and one-shot means no exploration turns to collapse. On local
  models pre-load's value is *accuracy* (largest where the query has no hint),
  not cost. The cost-saving story is a *cloud / strong-agent* property.

## Caveats / gotchas

- The GPU is **shared** — large models (32B fp8) OOM'd when other users' processes
  + the sweep's embedding model co-resided; tune `--gpu-memory-utilization` down
  and leave headroom for the embedding model (it lives on the same GPU).
- vLLM startup: port conflicts, sampler-warmup OOM (`--max-num-seqs` down), and
  transient GPU-memory-release races between successive serves.
- reps=1; base scored-n per model differs (42 / 35 / 61) as runs are independent.
