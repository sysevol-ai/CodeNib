# Frontend loop — running & iterating the web demo

How to run, screenshot, and self-critique CodeMiner's web demo (the
DeepWiki-style repo browser: **wiki / codemap / ask**). This is the playbook for
any agent doing a UI iteration loop. For where the product is *headed* (graph
overhaul, differentiation), see [`graph-frontend-direction.md`](../design/graph-frontend-direction.md).

> **The demo is on `main`** — FastAPI backend `codeminer/web/`, Next.js frontend
> `web/`, and the wiki layer `codeminer/wiki/` were merged via PR #166/#167. No
> branch switch is needed; work from the repo checkout. (The original iteration
> branch `claude/deepwiki-loop` is now effectively merged — only a 1-line diff in
> `codeminer/web/repo_registry.py` remains, so don't `git switch` to it.)

## Run it

Two processes: a FastAPI backend on **:8000** and a Next.js dev server on **:3000**.
Run the backend **from the repo root** (its `data_dir` is relative).

```bash
# --- backend (FastAPI / uvicorn) ---
export CODEMINER_DEMO_MODEL=openai/gpt-4o-mini   # REQUIRED: on-branch qa_config.yaml
                                                 # defaults to vertex_ai/gemini-2.5-flash,
                                                 # which needs gcloud ADC (not set here).
                                                 # OPENAI_API_KEY is set, so override to it.
PY=/home/zhongming/anaconda3/envs/codeminer/bin/python   # has litellm; `which python` in the
                                                         # codeminer conda env resolves here.
                                                         # System /usr/bin/python3 has NO litellm.
setsid bash -c "exec $PY -m uvicorn codeminer.web.app:app --host 127.0.0.1 --port 8000" \
  >/tmp/cm-web.log 2>&1 </dev/null &                # setsid + </dev/null so it survives the
                                                    # background-job wrapper exiting.
# The port stays CLOSED until indices finish loading (~20s) — the lifespan loads
# repos before uvicorn accepts connections, so an early curl gets connection-refused.
# Retry until healthy:
until curl -sf http://127.0.0.1:8000/api/health; do sleep 2; done   # -> {"status":"ok","repos":N}
```

```bash
# --- frontend (Next.js) ---
cd web
npm install            # first time only
npm run dev            # = `next dev`; binds :3000 (no port flag anywhere)
```

- The ASGI app is `app = FastAPI(...)` in `codeminer/web/app.py` (target `codeminer.web.app:app`).
  There is also a `codeminer-web` console script (honors `CODEMINER_DEMO_HOST`/`PORT`).
- **Repo pool**: 4 repos served from prebuilt vector stores under `/mnt/data/codeminer`
  (~840 per-instance dirs; `qa_config.yaml` sets `prebuilt_dir`, overridable via
  `CODEMINER_DEMO_PREBUILT_DIR`). Layout: `<dir>/<instance_id>/{repo,l0,l2}/index_<suffix>.faiss`.
- **Frontend → backend** is wired by `NEXT_PUBLIC_API_BASE` (default `http://127.0.0.1:8000`).
  Don't confuse it with `CODEMINER_DEMO_MODEL`, which only sets the backend's LLM.
- **Over SSH, forward both ports** — API calls run *in the browser*, not server-side:
  `ssh -L 3000:localhost:3000 -L 8000:localhost:8000 <host>`.

## Performance & caching (why a page can feel slow)

- **Wiki prose is LLM-generated and disk-cached** under `<data_dir>/wiki_cache/`
  (i.e. `.codeminer_qa/wiki_cache/agentwiki_<sha1(instance@commit/suffix)[:16]>.json`)
  — NOT under `/mnt/data/codeminer` (that holds the prebuilt graph + vectors).
  The **first** visit to an un-narrated page runs the model (~8–20s); after that
  it's ~2ms. The codemap / wiki-page subgraph is computed **dynamically** per
  request but is fast (~50–115ms), so it isn't cached.
- **Pre-warm the cache** so navigation is instant (run once per data_dir; ~minutes):
  ```bash
  B=http://127.0.0.1:8000; PY=<codeminer-conda-python>
  ids(){ curl -s "$B/api/repos/$1/wiki" | $PY -c "import sys,json
  def w(ps):
   for p in ps:
    print(p['id']); w(p.get('children',[]))
  w(json.load(sys.stdin).get('pages',[]))"; }
  for r in $(curl -s "$B/api/repos" | $PY -c "import sys,json;[print(x['id']) for x in json.load(sys.stdin)]"); do
    for p in $(ids "$r"); do curl -s -o /dev/null --max-time 120 "$B/api/repos/$r/wiki/$p"; done
  done
  ```
  To force re-narration after a prompt change, delete the matching
  `agentwiki_*.json` (outline key = `sha1("<instance>@<commit_short>/outline")[:16]`).
- **Frontend weight**: Mermaid (~1MB) and the Cytoscape graph are `next/dynamic`
  lazy-loaded, so the narrative paints first. Remember `next dev` is unminified —
  a production `next build && next start` is markedly faster than the dev server.

## Screenshot / visual verification (Playwright)

Playwright (chromium) is a `web/` devDep. Two ready scripts; **run them from inside
`web/`** (they `import { chromium } from "playwright"`, resolved from
`web/node_modules` — a script in `/tmp` or `$CLAUDE_JOB_DIR` fails with
`ERR_MODULE_NOT_FOUND`). Screenshots write to
`../verification/` (a sibling of `web/`, **not** in the repo — `mkdir` it first).

```bash
cd web && mkdir -p ../verification
# (1) live smoke test against the REAL running stack:
node verify.mjs http://127.0.0.1:3000/ home 1280 900
#     -> ../verification/home.png + JSON {httpStatus, horizontalScroll, consoleErrors, pageErrors}
# (2) offline flow test with a fully MOCKED backend (no server/LLM, but next dev must be up):
node qa_verify.mjs answer        # modes: loaded | answer | mobile | down
#     -> ../verification/<name>.png : qa-loaded / qa-answer / qa-mobile / qa-backend-down
#        (note: `down` writes qa-backend-down.png, NOT qa-down.png) + JSON {counts, consoleErrors}
```

Then `Read` the PNG to eyeball it. Both scripts launch headless chromium, `goto`
with `waitUntil:'networkidle'`, wait ~1.5s for mermaid/fetches to settle, screenshot
full-page, and print a JSON report (console/pageerror capture + a horizontal-overflow
check). New screenshot scripts should follow that shape.

> **No pixel-diff yet.** `pixelmatch` + `pngjs` are devDeps but **unused** — no
> baseline-diff step is implemented. If you add regression diffing, that's the hook.

## Operational gotchas

- **Kill by port, never `pkill -f uvicorn`** (mandatory here): the launching shell's
  command line matches the pattern, so `pkill` SIGTERMs *itself* (exit 144) and the
  server with it — **and** a foreign user's uvicorn already runs on this host, which
  `pkill` would also kill. Use:
  ```bash
  PID=$(ss -tlnp | awk '$4 ~ /:8000$/' | grep -oE 'pid=[0-9]+' | cut -d= -f2); [ -n "$PID" ] && kill $PID
  ```
- **Confirm litellm with `import litellm`**, not `litellm.__version__` (no such attr →
  false negative). For the version: `python -c "from importlib.metadata import version; print(version('litellm'))"`.

## Self-critique loop discipline (G5)

When iterating the UI against criteria:

- **Never declare "exit criteria met" on a *blessing* critique** ("no actionable
  findings"). Re-run an *adversarial* critique against the **live app** (screenshot
  it, attack it) before claiming convergence.
- **Don't loosen a criterion to make a new one pass.** If a probe's assumption breaks
  (e.g. an LLM rewrite moved the text a probe keyed on), fix the *probe location* and
  document it as a probe update — not a relaxation of the bar.

## What "good" looks like

Legible labels, real interactivity, and — the product's whole bet — **every claim
and every graph edge is clickable to the exact source line**. The current graph
(Mermaid auto-layout) does not meet this bar; the planned replacement does. See
[`graph-frontend-direction.md`](../design/graph-frontend-direction.md).
