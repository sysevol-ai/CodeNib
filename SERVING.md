<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Serving runbook (`serving-config` branch)

Server deployment of the CodeMiner web demo: FastAPI backend + **Claude Haiku 4.5 on
Vertex AI** for the Ask/QA + wiki generation. Config lives in `qa_config.yaml`.

> The qwen vLLM on `:8000` is a **separate** serving endpoint and is not used here.
> The backend runs on `:8080` to avoid that collision.

## 0. One-time env setup

```bash
conda activate codeminer-backend            # env with litellm / faiss / uvicorn
pip install -U "google-cloud-aiplatform>=1.38"   # litellm's Vertex path needs `vertexai`
gcloud auth application-default login        # Google ADC (once per machine)
```

Sanity-check Vertex works:
```bash
VERTEXAI_PROJECT=ucsd-cse-stable-gcp VERTEXAI_LOCATION=us-east5 \
python -c "import litellm; print(litellm.completion(model='vertex_ai/claude-haiku-4-5@20251001', messages=[{'role':'user','content':'say OK'}], max_tokens=8).choices[0].message.content)"
```

## 1. Index one or more repos (sparse / BM25)

`index_repo.py` clones-free: point it at an already-cloned repo; it builds the BM25
index and registers the repo in `.codeminer_qa/qa_registry.json`.

```bash
# small repo first, to validate the pipeline
git clone --depth 1 https://github.com/psf/requests ~/repos/requests
python scripts/index_repo.py ~/repos/requests

# then any others (vllm is large; expect a longer index)
# git clone --depth 1 https://github.com/vllm-project/vllm ~/repos/vllm
# python scripts/index_repo.py ~/repos/vllm
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

Run it inside `tmux` (e.g. `tmux new -s cm-backend`) so it survives SSH disconnects.

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
