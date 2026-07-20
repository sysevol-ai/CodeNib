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

| What | How it runs | Bind |
|---|---|---|
| Caddy reverse proxy (tunnel origin) | tmux `cm-caddy` | **127.0.0.1:7860** |
| Frontend (`next start`, prod build) | tmux `cm-frontend` | **127.0.0.1:3000** |
| Backend (`codeminer.web.app`) | tmux `cm-backend` | **127.0.0.1:8000** |
| vLLM Ask model (Qwen3.6-35B-A3B) | docker `vllm-qwen36` | **127.0.0.1:8080** |
| vLLM embeddings (Qwen3-Embedding-0.6B) | docker `vllm-embed` | **127.0.0.1:8081** |

## Public access & security (Cloudflare tunnel)

Public URL: **https://demo.codenib.ai** — served through a Cloudflare tunnel
(`dgx-codewiki`, token-managed; DNS/TLS/route configured in the Cloudflare dashboard,
pointing the hostname at `http://localhost:7860`).

**Everything binds `127.0.0.1` — nothing is on the public IP.** The only inbound path
is the Cloudflare tunnel (outbound-only from the DGX). This closes the bypass where
`132.239.17.29:<port>` would otherwise hit a service directly, skipping Cloudflare.

```
Internet ─HTTPS─► Cloudflare ─tunnel(outbound)─► Caddy 127.0.0.1:7860
                                                    ├─ /api/* → backend 127.0.0.1:8000
                                                    └─ /*     → frontend 127.0.0.1:3000
                                                                     └─ vLLM 127.0.0.1:8080 / :8081
```

Caddy (`~/Caddyfile`, tmux `cm-caddy`) does same-origin routing so the browser only ever
talks to `demo.codenib.ai` (no mixed content, no CORS):

```
:7860 {
    bind 127.0.0.1                       # loopback only — NOT ":7860" alone (that binds *:7860)
    handle /api/* { reverse_proxy 127.0.0.1:8000 }
    handle       { reverse_proxy 127.0.0.1:3000 }
}
```

Key details:
- **`bind 127.0.0.1`** is required. A bare `:7860` site binds `*:7860` (all interfaces) —
  the exact public bypass we're closing. Verify with `ss -tlnp | grep 7860` → must show
  `127.0.0.1:7860`, never `*:7860` / `0.0.0.0:7860`.
- Site address is **`:7860` (any Host)**, not `http://127.0.0.1:7860` — Caddy matches sites
  by Host header, and the tunnel sends `Host: demo.codenib.ai`; a `127.0.0.1` site returns
  an empty 200 for tunnel traffic.
- Frontend is built with **`NEXT_PUBLIC_API_BASE=http://127.0.0.1:7860`**: `web/lib/api.ts`
  same-origins in the browser when the base host is `127.0.0.1`/`localhost` (so the browser
  calls `demo.codenib.ai/api/...`), while SSR uses the full `127.0.0.1:7860` URL.
- **Docker port maps must carry the IP** (`-p 127.0.0.1:8080:8000`); a bare `-p 8080:8000`
  binds `0.0.0.0` and bypasses the firewall, exposing the GPU endpoints publicly.
- Backend reaches vLLM via `localhost:8080` / `:8081`, so loopback binding is transparent.

Bindings are enforced at process start: `run_backend.sh` (`CODEMINER_DEMO_HOST=127.0.0.1`),
`run_frontend.sh` (`next start -H 127.0.0.1`), `run_caddy.sh` (Caddyfile `bind 127.0.0.1`),
and the docker `-p 127.0.0.1:...` maps (containers are `--restart unless-stopped`).

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

## Agent runtime & compiler (which project components this uses)

- **Agent runtime:** `codeminer/agent/runner.py::AgentRunner` — built per repo in
  `codeminer/web/repo_registry.py` and driven by `POST /api/chat` → `bundle.runner.run()`.
  Wiki prose + edge labels run on a separate model via `wiki_model` (not the agent).
  This branch extends the runtime: `AgentRunner._force_final_answer` (synthesize a real
  answer when a run ends mid-exploration) and disabling Qwen thinking in
  `codeminer/llm/litellm_chat.py`.
- **Compiler:** `codeminer/compiler/IndexCompiler` → `RepoManifest`. BM25 is built by
  `scripts/index_repo.py`; the dense vector index by `~/build_vectors.py` (mirrors
  `scripts/build_qa_index.py`'s `VectorIndexBuilder` registration). Both call
  `IndexCompiler.compile_repo()` and write `.codeminer_cache/repo_manifest.json`, which
  the backend serves from. This uses the base `IndexCompiler` — **not** the
  `scripts/agent_compile/` RFC phase-lineage / compiled-subset tooling.

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

Indexed: 26 repos — `psf/requests` + all 5 language configs of
`sysevol-ai/codeminer-synthesis` (5 repos each: Python, C/C++, Go, Rust, TS/JS),
default branch, all `hybrid_search: true`.

## 2b. Symbol graph (codemap / figures) — SCIP toolchain

The wiki **graph/figure** views need the `symbol_graph` index (SCIP static analysis —
**no LLM**). `index_repo.py` and `build_vectors.py` do NOT build it; `~/build_full.py`
adds it (registers `SymbolGraphBuilder` and compiles `["bm25","vector","symbol_graph"]`
together — building `symbol_graph` alone would overwrite the manifest and drop bm25/vector).

Per-language tools, all installed **without sudo** under `~/codeminer-scip-tools`
(`CODEMINER_SCIP_TOOLS_DIR`) + a `scip-env` miniconda env. This box is **aarch64**:

| Lang | Tool | Install (aarch64) |
|---|---|---|
| Python | `scip-python` + miniconda `scip-env` + `protoc` | npm `@sourcegraph/scip-python`; miniconda (`conda tos accept` the default channels; `scip-env` = py3.11+pip+nodejs); protoc release binary |
| Go | `scip-go` | go1.26.4 linux-arm64 tarball → `go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7` |
| TS/JS | `scip-typescript` | npm `@sourcegraph/scip-typescript` (no per-repo `npm install` needed) |
| Rust | `rust-analyzer` | `rustup component add rust-analyzer`; set `CODEMINER_RUST_TOOLCHAIN=stable` (indexer defaults to nightly) |
| C/C++ | `clangd` | **NOT scip-clang** (no linux-arm64 build). CodeMiner routes C/C++ through clangd; install via `conda install -n scip-env -c conda-forge clang-tools clangdev` (arm64). No `compile_commands.json` needed — clangd background-indexes. |

Build (env sets tool PATH + conda + protoc; graph lang is the 2nd arg):

```bash
export CODEMINER_SCIP_TOOLS_DIR=~/codeminer-scip-tools GOROOT=$CODEMINER_SCIP_TOOLS_DIR/go \
  GOPATH=$CODEMINER_SCIP_TOOLS_DIR/go-tools CODEMINER_RUST_TOOLCHAIN=stable
export PATH="$CODEMINER_SCIP_TOOLS_DIR/bin:$CODEMINER_SCIP_TOOLS_DIR/go-tools/bin:$CODEMINER_SCIP_TOOLS_DIR/go/bin:$CODEMINER_SCIP_TOOLS_DIR/node-tools/node_modules/.bin:$CODEMINER_SCIP_TOOLS_DIR:$HOME/.cargo/bin:$HOME/miniconda3/envs/scip-env/bin:$PATH:$HOME/miniconda3/bin"
python ~/build_full.py <repo_dir> <python|go|typescript|rust|cpp>
```

Manifest then reports `symbol_navigation: true` and `/api/repos/<id>/codemap` returns
nodes/edges/mermaid. Driver scripts: `~/run_symgraph_py.sh`, `~/run_symgraph_multi.sh`
(Go/TS/Rust), `~/run_symgraph_cpp.sh`. **Restart `cm-backend` after building graphs.**

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
