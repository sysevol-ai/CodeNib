<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Serving runbook (`serving-config` branch)

Server deployment of the CodeMiner web demo, with two LLM backends split by role
(config: `qa_config.yaml`):

| Role | Model | Backend |
|---|---|---|
| **Ask** (interactive agent) | `openai/qwen3-coder-next` | local vLLM on `:8000` |
| **Wiki prose + edge labels** | `vertex_ai/claude-haiku-4-5@20251001` | Vertex AI (gcloud ADC) |

Backend runs on `:8080` (vLLM owns `:8000`). Indexes live under
`/home/boqin/data/codeminer-index`.

> **Note:** indexing (`scripts/index_repo.py`, BM25) calls **no LLM** — it's pure
> local compute. The two models above are only used at serve time.

> **Heads-up on Ask + qwen:** the Ask agent accumulates context over up to
> `max_turns` turns with no cap, so it can exceed the vLLM `--max-model-len`
> (65536) on large repos/long sessions. If you hit "maximum context length",
> lower `max_turns` / retrieval `top_k`, or move Ask to a larger-context model.

## 0. One-time env setup

```bash
conda activate codeminer-backend
pip install -U "google-cloud-aiplatform>=1.38"   # litellm's Vertex path needs `vertexai`
gcloud auth application-default login             # Google ADC (once per machine)
```

The vLLM qwen server must be running on `:8000` for Ask to work (separate process).

## 1. Index one or more repos (into your index dir)

`index_repo.py` builds BM25 for an already-cloned repo and registers it. Keep the
repos and the registry under `data_dir` so everything is in one place:

```bash
mkdir -p ~/data/codeminer-index/repos

git clone --depth 1 https://github.com/psf/requests ~/data/codeminer-index/repos/requests
python scripts/index_repo.py ~/data/codeminer-index/repos/requests \
  --registry ~/data/codeminer-index/qa_registry.json

# repeat for other repos (vllm is large; expect a longer index)
```

Result:
```
~/data/codeminer-index/
  qa_registry.json                    # registry (absolute paths — don't move repos after)
  wiki_cache/                         # wiki + edge-label cache (created at serve time)
  repos/requests/.codeminer_cache/    # BM25 index artifacts
```

## 2. Start the backend (port 8080, reachable by anyone)

```bash
VERTEXAI_PROJECT=ucsd-cse-stable-gcp \
VERTEXAI_LOCATION=us-east5 \
CODEMINER_DEMO_HOST=0.0.0.0 \
CODEMINER_DEMO_PORT=8080 \
CODEMINER_DEMO_CONFIG=qa_config.yaml \
python -m codeminer.web.app
```

Vertex creds come from the `VERTEXAI_*` env + ADC (for the wiki model). The qwen
creds (`OPENAI_API_BASE` / `OPENAI_API_KEY`) come from `qa_config.yaml`
(`api_base` / `api_key`). Run inside `tmux` (e.g. `tmux new -s cm-backend`).

## 3. Verify

```bash
curl -s http://localhost:8080/api/repos     # lists the repos you indexed (not [])
```

## 4. Frontend (optional, same server)

```bash
cd web
NEXT_PUBLIC_API_BASE=http://<server-host>:8080 npm run dev -- -H 0.0.0.0 -p 3000
```

`NEXT_PUBLIC_API_BASE` must be a URL the browser can reach (the server's public host,
not `localhost`). Later, put both behind a same-origin Caddy reverse proxy
(`/` -> :3000, `/api` -> :8080) to drop CORS and get automatic HTTPS.
