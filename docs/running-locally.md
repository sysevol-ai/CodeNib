<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Running the Web UI with a Local GPU or Hosted API

This guide runs generated Wiki pages and Ask against a local repository using a
local GPU LLM or a hosted API profile. The repository must contain at least one
[supported source language](language_capabilities.md), or you must pass an
explicit supported `--language`.

The setup has three services running in separate terminals:

| Service | Script | Where to run |
|---------|--------|--------------|
| LLM server (local profile only) | `scripts/start_llm.sh` | GPU node |
| CodeNib backend (FastAPI) | `scripts/start_web.sh` | Main machine |
| Vite frontend | `cd web && npm run dev` | Main machine |

---

## Prerequisites

### Main machine

- A CodeNib source checkout with development dependencies: `make dev`
- Node.js `^20.19.0` or `>=22.12.0`, plus npm: `make web-deps` (once)
- Conda env `codenib` active

### GPU node (local profile only)

- Access to a node with CUDA 12.4+ driver and enough VRAM (7B model needs ~5 GB)
- `llama-cpp-python[server]` with GPU support installed in the `codenib` env:

  ```bash
  # Install the pre-built CUDA 12.4 wheel (works with any CUDA 12.4+ driver)
  conda activate codenib
  pip install "llama-cpp-python[server]==0.3.29" \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
  ```

  Verify GPU is detected:
  ```bash
  python -c "import llama_cpp; print('GPU:', llama_cpp.llama_supports_gpu_offload())"
  # Should print: GPU: True
  ```

- A GGUF model file. If you have Ollama installed, qwen2.5-coder:7b is at:
  ```
  ~/.ollama/models/blobs/sha256-60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463
  ```
  Otherwise download any GGUF from HuggingFace and note the path.

---

## Step 1 — Index a repo

For a repository you want to explore, build its BM25 index and register it in
one command:

```bash
# Clone the repo (skip if already cloned)
git clone https://github.com/<owner>/<repo> ~/projects/<repo>

# Build the index and register the repo
conda activate codenib
cd ~/projects/CodeNib/CodeNib
python scripts/index_repo.py /absolute/path/to/your/repo
```

The script uses CodeNib's shared language registry to detect every supported
language represented by the repository's source extensions. Override detection
with `--language go`; comma- or slash-separated values such as
`javascript/typescript` also work. If detection finds nothing supported, the
script exits instead of silently indexing the repository as Python.

Indexes are written below
`$CODENIB_HOME/repositories/<repo>-<id>/indexes` (default
`~/.codenib/repositories/...`). The web registry defaults to
`.codenib_qa/qa_registry.json`; change that path with `--registry`. Restart an
already-running backend after registering a repository.

---

## Step 2 — Choose a model profile

Demo YAML supports relative inheritance through `extends`. The intended stack
keeps repository and retrieval settings separate from the generation route:

```text
qa_config.yaml                 shared defaults
└── qa_config.local.yaml       machine paths, indexes, embeddings, local LLM
    └── qa_config.api.yaml     hosted generation override
```

Child values override parents, nested mappings are merged, and environment
variables override the final merged YAML. This lets the API profile retain the
same indexes and cache as local serving without duplicating machine-specific
paths. An explicit YAML `null` clears an inherited endpoint, credential, or
option. Both profile files are ignored by Git.

### Local serve profile

No tracked config edit is required. `scripts/start_web.sh` points the backend at
the local OpenAI-compatible endpoint by exporting:

```bash
CODENIB_DEMO_MODEL=openai/qwen2.5-coder
OPENAI_API_BASE=http://<gpu-node>:8080/v1
```

For local-only config changes, copy the template and edit the ignored file:

```bash
cp qa_config.local.yaml.example qa_config.local.yaml
```

When present, `scripts/start_web.sh` automatically uses `qa_config.local.yaml`.
Override `CODENIB_DEMO_CONFIG` or `CODENIB_DEMO_MODEL` before running
`start_web.sh` if your local server exposes a different model name or config
path.

### Hosted API profile

Create the thin API overlay after the local profile, then provide the key only
through the environment:

```bash
cp qa_config.local.yaml.example qa_config.local.yaml
cp qa_config.api.yaml.example qa_config.api.yaml
export CODENIB_DEMO_API_KEY="$DEEPSEEK_API_KEY"
```

The supplied API profile routes both Ask and Wiki prose through DeepSeek while
reusing the local profile's repository registry, hybrid indexes, embedding
endpoint, and Wiki cache. Change only `qa_config.api.yaml` to use another
hosted provider.

---

## Step 3 — Start all three services

### Terminal 1 — LLM server (local profile only, on GPU node)

```bash
cd ~/projects/CodeNib/CodeNib
bash scripts/start_llm.sh
```

The script will ask for your GGUF model path and start an OpenAI-compatible
server on port 8080.

### Terminal 2 — CodeNib backend (on main machine)

Local serve:

```bash
cd ~/projects/CodeNib/CodeNib
bash scripts/start_web.sh
```

Hosted API service:

```bash
cd ~/projects/CodeNib/CodeNib
CODENIB_DEMO_PROFILE=api bash scripts/start_web.sh
```

The local profile asks for the GPU node hostname and points port 8000 at that
LLM server. The API profile loads `qa_config.api.yaml`, skips the GPU prompt,
and uses the configured hosted endpoint.

### Terminal 3 — Frontend (on main machine)

```bash
cd ~/projects/CodeNib/CodeNib/web
npm run dev
```

Opens at **http://localhost:3000**.

---

## Step 4 — Load or generate a page

1. Open http://localhost:3000
2. Click on your repo
3. Select a page
4. On its first request, wait while the backend generates and caches it

Wiki pages are cached under `<data_dir>/wiki_cache` (by default
`.codenib_qa/wiki_cache`) so subsequent loads avoid another model call.
**Refresh this wiki** only re-fetches the current tree and page; it does not
invalidate that cache or force generation. After repairing a model or index,
prefer the bounded `wiki-cache-prewarm --retry-degraded-now` maintenance path
shown below; it preserves healthy pages and retries only degraded entries.
Prompt/index identity changes naturally select a new cache key, so deleting the
whole cache should not be part of the normal workflow.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GPU: False` from llama_cpp | Wrong wheel installed. Reinstall with `--extra-index-url` as shown above. |
| `ContextWindowExceededError` | LLM server started without `--n_ctx 8192`. The start script sets this automatically. |
| `Connection refused` on port 8080 | LLM server not running, or firewall blocking the GPU node port. Check Terminal 1. |
| Wiki says "Couldn't load this page" | Read the HTTP status and backend detail displayed in the page, then check the backend log. Provider/generation failures point to the Wiki model. A legacy source-fingerprint warning means vector retrieval was safely skipped and BM25 remains available; rebuild the manifest and vector artifact with the current CodeNib version to restore hybrid retrieval. |
| `repos: 0` at `/api/health` | `qa_registry.json` missing or wrong path. Check `.codenib_qa/qa_registry.json`. |
| Backend stuck on "Loading repositories…" | Index not built. Run Step 1 again. |
| Frontend still shows old code | Inspect the Vite cwd with `make web-status`. Stop a current-user process rooted in an old checkout or detached snapshot and restart from the checkout being edited; do not reclaim another user's listener. |
| Wiki still shows degraded text after fixing the model | Run `make wiki-cache-prewarm WIKI_CACHE_PREWARM_ARGS="--config <config> --scope overview --workers 2 --retry-degraded-now"`. This explicitly retries degraded pages without deleting healthy cache entries. |

---

## Running over SSH

With the default or another loopback API configuration, forward port 3000
only:

```bash
ssh -L 3000:localhost:3000 <main-machine>
```

The browser talks same-origin to the Vite dev server, which proxies
`/api/*` server-side to the FastAPI backend at `CODENIB_API_BASE` (default
`http://127.0.0.1:8000`; see `web/vite.config.ts`) — port 8000 does not need to
be forwarded.

If the LLM server is on a different node than the backend, only the backend
needs to reach port 8080 on the GPU node — the browser never talks to port 8080
directly.
