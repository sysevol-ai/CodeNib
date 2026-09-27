# Frontend loop — running & iterating the web demo

How to run, screenshot, and self-critique CodeNib's web demo (the
DeepWiki-style repo browser: **wiki / codemap / ask**). This is the playbook for
any agent doing a UI iteration loop. For where the product is *headed* (graph
overhaul, differentiation), see [`graph-frontend-direction.md`](../design/graph-frontend-direction.md).

> **The demo is on `main`** — FastAPI backend `codenib/web/`, Next.js frontend
> `web/`, and the wiki layer `codenib/wiki/` were merged via PR #166/#167. No
> branch switch is needed; work from the repo checkout. (The original iteration
> branch `claude/deepwiki-loop` is now effectively merged — only a 1-line diff in
> `codenib/web/repo_registry.py` remains, so don't `git switch` to it.)

## Run it

Two processes: a FastAPI backend on **:8000** and a Next.js dev server on **:3000**.
Run the backend **from the repo root** (its `data_dir` is relative).

```bash
# --- backend (FastAPI / uvicorn) ---
export CODENIB_DEMO_MODEL=openai/gpt-4o-mini   # REQUIRED: on-branch qa_config.yaml
                                                 # defaults to vertex_ai/gemini-2.5-flash,
                                                 # which needs gcloud ADC (not set here).
                                                 # OPENAI_API_KEY is set, so override to it.
PY="${PY:-python}"   # use the active environment's interpreter
                                                         # codenib conda env resolves here.
                                                         # System /usr/bin/python3 has NO litellm.
setsid bash -c "exec $PY -m uvicorn codenib.web.app:app --host 127.0.0.1 --port 8000" \
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

- The ASGI app is `app = FastAPI(...)` in `codenib/web/app.py` (target `codenib.web.app:app`).
  There is also a `codenib-web` console script (honors `CODENIB_DEMO_HOST`/`PORT`).
- **Repo pool**: 4 repos served from prebuilt vector stores under `${CODENIB_PREBUILT_DIR}`
  (~840 per-instance dirs; `qa_config.yaml` sets `prebuilt_dir`, overridable via
  `CODENIB_DEMO_PREBUILT_DIR`). Layout: `<dir>/<instance_id>/{repo,l0,l2}/index_<suffix>.faiss`.
- **Frontend → backend** is wired by `NEXT_PUBLIC_API_BASE` (default `http://127.0.0.1:8000`).
  Don't confuse it with `CODENIB_DEMO_MODEL`, which only sets the backend's LLM.
- **Over SSH with the default or another loopback API base**, forward port
  3000 only. The browser talks same-origin to the frontend, and the Next.js
  dev server proxies `/api/*` to `CODENIB_API_BASE` (default
  `http://127.0.0.1:8000`) server-side: `ssh -L 3000:localhost:3000 <host>`.
  A non-loopback `NEXT_PUBLIC_API_BASE` bypasses that proxy and must be
  browser-reachable with suitable CORS configuration.

## Performance & caching (why a page can feel slow)

- **Repository cards** reuse source-derived summaries for the lifetime of a
  `RepoBundle`. Startup prepares those summaries before accepting requests;
  replacing an indexed generation gives it a fresh cache. Authenticated source
  reads still run on the first computation, failures can retry, and a newly
  cached Wiki lead takes precedence on the next list request. Avoid reading
  each package manifest again on every `/api/repos` call: each authenticated
  read verifies the source inventory, which made a 27-repository list take
  roughly 10 seconds. Warm requests after this fix measured 27–39 ms locally.
- **Browser repository lists** share an in-flight request and reuse a successful
  response for 60 seconds across home, Wiki, and Ask navigation. Failed requests
  are evicted; explicit refresh bypasses the cache. Consumers with an abort
  signal own an independent request so dialog cancellation cannot cancel page
  navigation. This cache does not pre-generate cold Wiki pages.
- **Cold large-repository Wiki loads** batch symbol source reads through the
  borrowed reader's authenticated session. Keep inventory checks before and
  after the batch and publish the symbol cache only after successful exit;
  otherwise per-symbol whole-tree scans can occupy a generator for minutes.
  Concurrent readers receive 503 with `Retry-After` after a bounded lock wait,
  and the browser retries at most five times. Validate cold overviews on a
  large repository as well as cached Requests pages before deploying.
- **System maps** assign indexed symbols using files in the cached outline,
  including child-page file assignments. They must work before child prose or
  citation caches exist, without fanning out retrieval across cold pages.
  Only authenticated source paths and recorded graph edges contribute. The
  first area listing a shared file owns its symbols. Concurrent graph readers
  wait for validation to finish; loading, unavailable graphs, and no recorded
  cross-area calls have distinct UI states instead of silently hiding the map.
- **Deployment acceptance** checks cache coverage across the entire page tree
  and System Map availability for every served repository. Successful Overview
  requests do not establish that child pages are cached or maps are present.
  Use `scripts/prewarm_wiki_cache.py --scope all` for full prose coverage;
  report cold, degraded, and failed pages explicitly while prewarming runs.
- **Visitor Wiki acceptance** includes a source failure before the first
  chapter. Show the stopped state and reason prominently, remove active/waiting
  cues, and preserve the owner's explicit retry path. Exercise oversized files
  beside valid source: skip them without decompression, validate their paths,
  retain archive bounds, and disclose the omissions in the saved Wiki.
- **Overview illustrations** require an admitted semantic architecture plan.
  Do not substitute an automatic call-flow or relation image when that plan
  is missing or invalid: a debug/configuration branch can look like the main
  system path. Keep indexed System Maps separate. Bump the media-plan version
  when changing this policy so existing pages drop obsolete derived assets
  without invalidating cached prose.
- **Wiki prose is LLM-generated and stored** in
  `<data_dir>/wiki_cache/wiki.sqlite3` — NOT under `${CODENIB_PREBUILT_DIR}`
  (that holds the prebuilt graph + vectors).
  The **first** visit to an un-narrated page runs the model (~8–20s); after that
  it's ~2ms. The codemap / wiki-page subgraph is computed **dynamically** per
  request but is fast (~50–115ms), so it isn't cached.
- **Pre-warm the cache** so navigation is instant (run once per data_dir; ~minutes):
  ```bash
  B=http://127.0.0.1:8000; PY=<codenib-conda-python>
  ids(){ curl -s "$B/api/repos/$1/wiki" | $PY -c "import sys,json
  def w(ps):
   for p in ps:
    print(p['id']); w(p.get('children',[]))
  w(json.load(sys.stdin).get('pages',[]))"; }
  for r in $(curl -s "$B/api/repos" | $PY -c "import sys,json;[print(x['id']) for x in json.load(sys.stdin)]"); do
    for p in $(ids "$r"); do curl -s -o /dev/null --max-time 120 "$B/api/repos/$r/wiki/$p"; done
  done
  ```
  Prompt or index identity changes select a fresh entry automatically. To
  retry degraded entries without discarding healthy pages, use
  `wiki-cache-prewarm --retry-degraded-now`.
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
