<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Running the Web Demo Locally (with a Local GPU LLM)

This guide covers running the full CodeNib web demo — wiki generation + Ask
— against **any arbitrary repo** using a **local GPU LLM** (no cloud API keys).

The setup has three services running in separate terminals:

| Service | Script | Where to run |
|---------|--------|--------------|
| LLM server (llama-cpp-python) | `scripts/start_llm.sh` | GPU node |
| CodeNib backend (FastAPI) | `scripts/start_web.sh` | Main machine |
| Vite frontend | `cd web && npm run dev` | Main machine |

---

## Prerequisites

### Main machine
- A CodeNib source checkout with development dependencies: `make dev`
- Node.js + npm: `make web-deps` (once)
- Conda env `codenib` active

### GPU node
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

For any repo you want to explore, build its BM25 index and register it in one
command:

```bash
# Clone the repo (skip if already cloned)
git clone https://github.com/<owner>/<repo> ~/projects/<repo>

# Build the index and register the repo
conda activate codenib
cd ~/projects/CodeNib/CodeNib
python scripts/index_repo.py /absolute/path/to/your/repo
```

The script auto-detects the language from file extensions (override with
`--language go` — comma- or slash-separated values such as
`javascript/typescript` also work), builds the BM25 index under
`~/.codenib/repositories/<repo>-<id>/indexes`, and registers the repo in
`.codenib_qa/qa_registry.json` (change the path with `--registry`). Restart
the backend afterwards to pick up the new repo.

### What happens underneath (manual flow)

`scripts/index_repo.py` drives `IndexCompiler` and writes the registry entry
for you. Doing the same by hand:

```bash
conda activate codenib
cd ~/projects/CodeNib/CodeNib
python - <<'EOF'
from codenib.compiler import IndexCompiler, IndexCompilerConfig
from codenib.compiler.index_builders import IndexBuilderRegistry, register_default_builders
from codenib.paths import repo_index_dir

REPO = "/absolute/path/to/your/repo"   # <-- change this
CACHE = repo_index_dir(REPO)

registry = IndexBuilderRegistry()
register_default_builders(registry, languages=["python"])  # change language if needed
IndexCompiler(registry, IndexCompilerConfig(index_types=["bm25"])).compile_repo(
    REPO, cache_dir=str(CACHE)
)
print("Done! Index at", CACHE)
EOF
```

Then register the repo in `.codenib_qa/qa_registry.json` (create the file if
it doesn't exist):

```json
[
  {
    "instance_id": "owner__repo",
    "repo": "owner/repo",
    "base_commit": "<git rev-parse HEAD of the repo>",
    "language": "python",
    "repo_dir": "/absolute/path/to/your/repo",
    "manifest_path": "/home/you/.codenib/repositories/repo-id/indexes/repo_manifest.json",
    "problem_statement": ""
  }
]
```

Get `base_commit` with:
```bash
git -C /path/to/repo rev-parse HEAD
```

---

## Step 2 — Configure the LLM

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

---

## Step 3 — Start all three services

### Terminal 1 — LLM server (on GPU node)

```bash
cd ~/projects/CodeNib/CodeNib
bash scripts/start_llm.sh
```

The script will ask for your GGUF model path and start an OpenAI-compatible
server on port 8080.

### Terminal 2 — CodeNib backend (on main machine)

```bash
cd ~/projects/CodeNib/CodeNib
bash scripts/start_web.sh
```

The script asks for the GPU node hostname (default: `vscode-dsmlp-l40s`) and
starts the FastAPI backend on port 8000 pointed at your LLM server.

### Terminal 3 — Frontend (on main machine)

```bash
cd ~/projects/CodeNib/CodeNib/web
npm run dev
```

Opens at **http://localhost:3000**.

---

## Step 4 — Generate the wiki

1. Open http://localhost:3000
2. Click on your repo
3. Click **Refresh this wiki**
4. Wait ~30–60 seconds — the LLM generates the outline and pages

Wiki pages are cached in `.codenib_qa/wiki_cache/` so subsequent loads are instant.
To regenerate, delete that directory.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GPU: False` from llama_cpp | Wrong wheel installed. Reinstall with `--extra-index-url` as shown above. |
| `ContextWindowExceededError` | LLM server started without `--n_ctx 8192`. The start script sets this automatically. |
| `Connection refused` on port 8080 | LLM server not running, or firewall blocking the GPU node port. Check Terminal 1. |
| Wiki says "Couldn't load this page" | Check backend terminal for `WARNING outline generation failed`. Usually an LLM connectivity issue. |
| `repos: 0` at `/api/health` | `qa_registry.json` missing or wrong path. Check `.codenib_qa/qa_registry.json`. |
| Backend stuck on "Loading repositories…" | Index not built. Run Step 1 again. |
| Blank wiki after "Refresh" | Wiki cached from a failed run. Delete `.codenib_qa/wiki_cache/` and retry. |

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
