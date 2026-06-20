<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Running the Web Demo Locally (with a Local GPU LLM)

This guide covers running the full CodeMiner web demo — wiki generation + Ask
— against **any arbitrary repo** using a **local GPU LLM** (no cloud API keys).

The setup has three services running in separate terminals:

| Service | Script | Where to run |
|---------|--------|--------------|
| LLM server (llama-cpp-python) | `scripts/start_llm.sh` | GPU node |
| CodeMiner backend (FastAPI) | `scripts/start_web.sh` | Main machine |
| Next.js frontend | `cd web && npm run dev` | Main machine |

---

## Prerequisites

### Main machine
- CodeMiner installed: `make dev` or `pip install -e ".[dev]"`
- Node.js + npm: `make web-deps` (once)
- Conda env `codeminer` active

### GPU node
- Access to a node with CUDA 12.4+ driver and enough VRAM (7B model needs ~5 GB)
- `llama-cpp-python[server]` with GPU support installed in the `codeminer` env:

  ```bash
  # Install the pre-built CUDA 12.4 wheel (works with any CUDA 12.4+ driver)
  conda activate codeminer
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

For any repo you want to explore, build its BM25 index:

```bash
# Clone the repo (skip if already cloned)
git clone https://github.com/<owner>/<repo> ~/projects/<repo>

# Build indexes
conda activate codeminer
cd ~/projects/CodeMiner/CodeMiner
python - <<'EOF'
from codeminer.compiler import IndexCompiler, IndexCompilerConfig
from codeminer.compiler.index_builders import IndexBuilderRegistry, register_default_builders

REPO = "/absolute/path/to/your/repo"   # <-- change this

registry = IndexBuilderRegistry()
register_default_builders(registry, languages=["python"])  # change language if needed
IndexCompiler(registry, IndexCompilerConfig(index_types=["bm25"])).compile_repo(REPO)
print("Done! Index at", REPO + "/.codeminer_cache/")
EOF
```

Then register the repo in `.codeminer_qa/qa_registry.json` (create the file if
it doesn't exist):

```json
[
  {
    "instance_id": "owner__repo",
    "repo": "owner/repo",
    "base_commit": "<git rev-parse HEAD of the repo>",
    "language": "python",
    "repo_dir": "/absolute/path/to/your/repo",
    "manifest_path": "/absolute/path/to/your/repo/.codeminer_cache/repo_manifest.json",
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
CODEMINER_DEMO_MODEL=openai/qwen2.5-coder
OPENAI_API_BASE=http://<gpu-node>:8080/v1
```

For local-only config changes, copy the template and edit the ignored file:

```bash
cp qa_config.local.yaml.example qa_config.local.yaml
```

When present, `scripts/start_web.sh` automatically uses `qa_config.local.yaml`.
Override `CODEMINER_DEMO_CONFIG` or `CODEMINER_DEMO_MODEL` before running
`start_web.sh` if your local server exposes a different model name or config
path.

---

## Step 3 — Start all three services

### Terminal 1 — LLM server (on GPU node)

```bash
cd ~/projects/CodeMiner/CodeMiner
bash scripts/start_llm.sh
```

The script will ask for your GGUF model path and start an OpenAI-compatible
server on port 8080.

### Terminal 2 — CodeMiner backend (on main machine)

```bash
cd ~/projects/CodeMiner/CodeMiner
bash scripts/start_web.sh
```

The script asks for the GPU node hostname (default: `vscode-dsmlp-l40s`) and
starts the FastAPI backend on port 8000 pointed at your LLM server.

### Terminal 3 — Frontend (on main machine)

```bash
cd ~/projects/CodeMiner/CodeMiner/web
npm run dev
```

Opens at **http://localhost:3000**.

---

## Step 4 — Generate the wiki

1. Open http://localhost:3000
2. Click on your repo
3. Click **Refresh this wiki**
4. Wait ~30–60 seconds — the LLM generates the outline and pages

Wiki pages are cached in `.codeminer_qa/wiki_cache/` so subsequent loads are instant.
To regenerate, delete that directory.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GPU: False` from llama_cpp | Wrong wheel installed. Reinstall with `--extra-index-url` as shown above. |
| `ContextWindowExceededError` | LLM server started without `--n_ctx 8192`. The start script sets this automatically. |
| `Connection refused` on port 8080 | LLM server not running, or firewall blocking the GPU node port. Check Terminal 1. |
| Wiki says "Couldn't load this page" | Check backend terminal for `WARNING outline generation failed`. Usually an LLM connectivity issue. |
| `repos: 0` at `/api/health` | `qa_registry.json` missing or wrong path. Check `.codeminer_qa/qa_registry.json`. |
| Backend stuck on "Loading repositories…" | Index not built. Run Step 1 again. |
| Blank wiki after "Refresh" | Wiki cached from a failed run. Delete `.codeminer_qa/wiki_cache/` and retry. |

---

## Running over SSH

If accessing from a remote machine, forward both ports — the browser calls the
backend directly:

```bash
ssh -L 3000:localhost:3000 -L 8000:localhost:8000 <main-machine>
```

If the LLM server is on a different node than the backend, only the backend
needs to reach port 8080 on the GPU node — the browser never talks to port 8080
directly.
