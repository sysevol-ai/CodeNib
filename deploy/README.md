<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Deploying CodeNib serving behind LiteLLM

This directory wires the CodeNib serving endpoint behind a [LiteLLM](https://github.com/BerriAI/litellm)
gateway so a code agent can call it exactly as it would call OpenAI — with an
automatic fallback to a plain model if CodeNib serving is unavailable.

```
agent ──POST /v1/chat/completions──▶ LiteLLM ──POST /v1/chat/completions──▶ codenib-serve ──▶ HF model (GPU)
                                        └── fallback ─────────────────────▶ plain model
```

Both hops speak the **same** OpenAI protocol; LiteLLM only decides *which backend*
answers and handles retries/fallback. (CodeNib retrieval is a separate,
in-process call inside `codenib-serve` — it does **not** go through LiteLLM.)

## 1. Start the CodeNib serving endpoint

```bash
pip install -e ".[serving]"
codenib-serve --model Qwen/Qwen2.5-Coder-0.5B --device cuda --port 8000
# GET http://localhost:8000/health  ->  {"status":"ok"}
```

The server binds to `127.0.0.1` by default. If a gateway runs on another host,
put the endpoint behind an authenticated network boundary before passing
`--host 0.0.0.0`; the serving process does not implement API-key authentication.

## 2. Start the LiteLLM gateway

```bash
pip install litellm
litellm --config deploy/litellm.config.yaml --port 4000
```

Edit `litellm.config.yaml` to point the **fallback** entry at your baseline
(a second endpoint, a vLLM server, or a hosted API).

## 3. Smoke test — same request shape reaches CodeNib serving

```bash
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "codenib-qwen-coder",
        "messages": [{"role": "user", "content": "Finish this:\n\ndef add(a, b):"}]
      }' | jq .
```

Streaming:

```bash
curl -N http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"codenib-qwen-coder","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

## Notes

- **Greedy only (v1).** A request with `temperature > 0` (or `top_p < 1`) is
  rejected with HTTP 400 — CodeNib serving serves lossless greedy decoding. Sampling is
  a future track.
- **Strict request subset.** Unknown fields and tool-calling fields are rejected
  rather than silently ignored. The request `model` must match the model listed
  by `/v1/models` (or the explicit `--served-model-name`).
- **Bounded requests.** Message bodies, completion tokens, tokenizer work, and
  streaming buffers are capped. Prompt plus completion must fit the target
  model's advertised context capacity. The server defaults to an 8,192-token
  budget because the eager tree-attention prefill is quadratic;
  `--max-context-tokens` can explicitly select up to the smaller of 32,768 and
  the model/tokenizer capacity.
- **Fallback demo.** Kill `codenib-serve` and repeat the smoke test: LiteLLM
  routes the same request to the fallback backend instead.
