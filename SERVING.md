<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Serving runbook (`serving-config` branch)

DGX Spark deployment of the CodeMiner DeepWiki demo. Everything runs on
`stable-spark1.ucsd.edu` as user `boqin`, in a **venv** (no conda, no sudo):
`source ~/codeminer-backend/bin/activate`.

## Architecture

```
Browser
  └─ Frontend  (Next.js, :3000, production build)
       └─ Backend  (FastAPI, :8000)
            ├─ Ask agent        → vLLM  Qwen3.6-35B-A3B      (:8080, docker)   answers
            ├─ Wiki + edge labels → Vertex  Claude Haiku 4.5  (ADC)            wiki prose
            └─ Retrieval (hybrid) → BM25 (local)  +  Qwen3-Embedding-0.6B      dense search
                                                     (vLLM :8081, docker)
```

## Ports & processes

| What | How it runs | Port |
|---|---|---|
| Frontend (`next start`, prod build) | tmux `cm-frontend` | 3000 |
| Backend (`codeminer.web.app`) | tmux `cm-backend` | 8000 |
| vLLM Ask model (Qwen3.6-35B-A3B) | docker `vllm-qwen36` | 8080 |
| vLLM embeddings (Qwen3-Embedding-0.6B) | docker `vllm-embed` | 8081 |

Public: frontend **http://stable-spark1.ucsd.edu:3000**, backend `:8000`.

## Models

| Role | Model string | Backend |
|---|---|---|
| Ask (interactive agent) | `openai/qwen3.6-35b` (served from `Qwen3.6-35B-A3B-FP8` + MTP) | vLLM `:8080` |
| Wiki prose + edge labels | `vertex_ai/claude-haiku-4-5@20251001` | Vertex (ADC) |
| Dense embeddings (hybrid retrieval) | `Qwen/Qwen3-Embedding-0.6B` (openai provider) | vLLM `:8081` |

**Ask throughput (GB10 is bandwidth-bound):** bf16 ~20 tok/s → **FP8 + MTP ~50-65 tok/s**
(~3×). MTP `num_speculative_tokens=1` (this model has one MTP layer; 2 is *slower*).
NVFP4 was tried but the vLLM `qwen3_5` loader can't map its expert scales, so FP8 is the
path. Qwen3.6 is a *thinking* model — thinking is disabled for the agent (it otherwise
emits a plain-text "Here's a thinking process:" preamble that breaks answers and wastes
tokens); the agent also force-synthesizes a final answer if it runs out of turns
mid-exploration. See `codeminer/llm/litellm_chat.py` + `codeminer/agent/runner.py`.

## 0. One-time env

```bash
source ~/codeminer-backend/bin/activate
pip install -U "google-cloud-aiplatform>=1.38"   # Vertex via litellm
gcloud auth application-default login             # Vertex ADC
# node via nvm (frontend): curl -o- .../nvm/install.sh | bash ; nvm install --lts
```

## 1. vLLM containers (docker, GPU)

```bash
# Ask model — FP8 MoE + MTP speculative decoding, qwen3_xml tool parser
docker run -d --name vllm-qwen36 --restart unless-stopped --gpus all --ipc=host \
  -p 8080:8000 -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:cu130-nightly \
  Qwen/Qwen3.6-35B-A3B-FP8 --served-model-name qwen3.6-35b \
  --max-model-len 65536 --gpu-memory-utilization 0.80 --max-num-seqs 2 \
  --enable-auto-tool-choice --tool-call-parser qwen3_xml \
  --speculative-config '{"method": "mtp", "num_speculative_tokens": 1}'

# Embedding model — pooling runner, small GPU slice
docker run -d --name vllm-embed --restart unless-stopped --gpus all --ipc=host \
  -p 8081:8000 -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:cu130-nightly \
  Qwen/Qwen3-Embedding-0.6B --runner pooling --gpu-memory-utilization 0.08
```

## 2. Index repos (hybrid = BM25 + dense vectors)

Indexes live under `~/data/codeminer-index` (`data_dir`). BM25 is pure-local;
the dense vectors are embedded through the `:8081` endpoint (GPU, not CPU — the
in-process SentenceTransformer path needs python-dev headers we can't `sudo`-install).

```bash
# a) clone default branch + build BM25 (registers the repo)
git clone --depth 1 https://github.com/<owner>/<repo> ~/data/codeminer-index/repos/<owner>_<repo>
python scripts/index_repo.py ~/data/codeminer-index/repos/<owner>_<repo> \
  --registry ~/data/codeminer-index/qa_registry.json

# b) add the dense vector index (rebuilds bm25 + vector via the embedding endpoint)
python ~/build_vectors.py ~/data/codeminer-index/repos/<owner>_<repo>
```

`~/build_vectors.py` registers a `VectorIndexBuilder` with
`embedding_provider="openai"`, `embedding_kwargs={"base_url":"http://localhost:8081/v1","api_key":"dummy"}`
and runs `IndexCompiler(... index_types=["bm25","vector"]).compile_repo(...)`. A repo's
manifest then reports `hybrid_search: true`. **Restart `cm-backend` after (re)indexing.**

Currently indexed (6): `requests` + the 5 Python repos from
`sysevol-ai/codeminer-synthesis` (astropy, matplotlib, xarray, scikit-learn, sympy).

## 3. Config — `qa_config.yaml`

Key fields for hybrid serving (already set on this branch):
`mode: hybrid`, `embedding_model: Qwen/Qwen3-Embedding-0.6B`, `embedding_dimension: 1024`,
`embedding_provider: openai`, `embedding_base_url: http://localhost:8081/v1`,
`model: openai/qwen3.6-35b`, `api_base: http://localhost:8080/v1`,
`wiki_model: vertex_ai/claude-haiku-4-5@20251001`, `data_dir: /home/boqin/data/codeminer-index`.

## 4. Serve

```bash
# backend (tmux cm-backend) — Vertex env for the wiki model; qwen/embed creds come from the yaml
VERTEXAI_PROJECT=ucsd-cse-stable-gcp VERTEXAI_LOCATION=us-east5 \
CODEMINER_DEMO_HOST=0.0.0.0 CODEMINER_DEMO_PORT=8000 \
CODEMINER_DEMO_CONFIG=qa_config.yaml \
python -m codeminer.web.app

# frontend (tmux cm-frontend) — production build (not `next dev`; dev compiles per-visit)
cd web && NEXT_PUBLIC_API_BASE=http://stable-spark1.ucsd.edu:8000 npm run build
NEXT_PUBLIC_API_BASE=http://stable-spark1.ucsd.edu:8000 npm run start -- -H 0.0.0.0 -p 3000
```

Helper scripts on the box: `~/run_backend.sh`, `~/run_frontend.sh`. Logs:
`~/cm-backend.log`, `~/cm-frontend.log`.

## 5. Pre-generate wikis (so first repo-open is instant)

The wiki is generated on-demand (Vertex Haiku) and cached to
`~/data/codeminer-index/wiki_cache/`. Pre-warm all repos with `~/pregen_wiki.sh`
(walks `/api/repos/<id>/wiki` + every page). Only the Ask answer stays on-demand.

## Notes / gotchas

- tmux sessions survive SSH drops, not reboots. Docker containers auto-restart.
- Shared HF cache `~/.cache/huggingface/hub` is root-owned (vLLM writes as root);
  for user-side HF downloads (e.g. `datasets`) set `HF_HOME=~/data/hf-cache`.
- Ask answers are grounded in BM25 + dense-retrieved code (hybrid). Thinking is
  disabled and the agent force-synthesizes a final answer, so replies are clean
  and complete (verified on requests + matplotlib).
- **Known issue:** wiki outline generation returns 0 pages for the largest repos
  (astropy, matplotlib, scikit-learn, sympy) — under investigation.
- **Known issue:** sympy is BM25-only (a chunk exceeds the 32768-token embed limit).
