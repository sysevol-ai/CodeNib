<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# DeepWiki-style Web Demo

A browsable, DeepWiki-style site over a fixed pool of indexed repositories. It
has two halves sharing one backend:

- **Wiki / Diagram** — an auto-generated page tree per repo with source-grounded
  prose, syntax-highlighted code references anchored to real symbol spans, and
  Mermaid diagrams.
- **Ask** — the agent QA flow: ask a question about a repo, get an answer with
  citation cards.

The stack is a FastAPI backend (`codeminer.web.app`) plus a Next.js frontend
(`web/`).

## Prerequisites

- CodeMiner installed (`make dev` or `pip install -e ".[dev]"`).
- Node.js (for the frontend). Install deps once: `cd web && npm install`.
- An LLM provider reachable via litellm, for Ask answers and wiki narration:
  - `OPENAI_API_KEY` for `openai/gpt-4o-mini` (a non-thinking model), or
  - gcloud Application Default Credentials for `vertex_ai/gemini-2.5-flash`
    (`gcloud auth application-default login`). The demo reads the project from
    the ADC `quota_project_id`; no extra env vars are required.

  The site still loads (repo list, wiki pages) without credentials, but Ask
  returns empty and wiki prose falls back to deterministic templated text.

## 1. Build (or reuse) the index

The server reads a registry written by the build script:

```bash
python scripts/build_qa_index.py        # needs network: HuggingFace + git
```

This selects instances from `codeminer-base-dataset`, checks out each repo at
its `base_commit`, builds indexes under `data_dir` (default `.codeminer_qa/`),
and writes `qa_registry.json`.

**Reusing pre-built indexes.** If a read-only tree of per-instance artifacts
already exists (layout `<dir>/<instance_id>/{repo,l0,l2}/...`), point
`prebuilt_dir` at it to skip cloning and embedding — only BM25 is built locally.
Set it in `qa_config.yaml` or via env:

```bash
export CODEMINER_DEMO_PREBUILT_DIR=/path/to/prebuilt
```

The path is fully configurable; nothing is hardcoded to a specific machine.

## 2. Launch the backend

From the repository root (so the relative `data_dir` resolves):

```bash
# autoreload during dev:
uvicorn codeminer.web.app:app --host 127.0.0.1 --port 8000
# or the console script:
codeminer-web                            # honors CODEMINER_DEMO_HOST/PORT
```

Index loading takes ~20s. Check it is up:

```bash
curl -s http://127.0.0.1:8000/api/health   # {"status":"ok","repos":N}
```

Config lives in `qa_config.yaml` (override path with `CODEMINER_DEMO_CONFIG`).
Key env overrides: `CODEMINER_DEMO_MODEL`, `CODEMINER_DEMO_DATA_DIR`,
`CODEMINER_DEMO_PREBUILT_DIR`.

## 3. Launch the frontend

```bash
cd web
npm run dev                              # Next.js dev server on :3000
```

Open [http://localhost:3000](http://localhost:3000). The frontend calls the
backend at `http://127.0.0.1:8000` by default; override with
`NEXT_PUBLIC_API_BASE`.

## Running over SSH

The API calls run **in your browser**, not on the server, so forward *both*
ports — not just 3000:

```bash
ssh -L 3000:localhost:3000 -L 8000:localhost:8000 <host>
```

If you forward only 3000, the page loads but hangs on **"Loading
repositories…"** — the browser's `127.0.0.1:8000` has nowhere to go. (`Address
already in use` on reconnect just means an earlier tunnel still holds the port.)

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Stuck on "Loading repositories…" | Backend unreachable from the browser. Confirm `:8000` is up and (over SSH) forwarded. Hard-refresh after the backend finishes loading. |
| Ask returns an empty answer | No usable LLM credentials, or a thinking model exhausting its token budget on hidden reasoning. The demo disables thinking for `gemini-2.5*` automatically; for other reasoning models raise `max_tokens`. |
| Wiki prose looks generic/templated | Narrator had no usable creds and fell back to templates. Wiki cache is keyed without the model, so clear `<data_dir>/wiki_cache` after changing models. |
| `repos: 0` at `/api/health` | No registry. Run `scripts/build_qa_index.py` (or fix `data_dir`/`prebuilt_dir`). |
