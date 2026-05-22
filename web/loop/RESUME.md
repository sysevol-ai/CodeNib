<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Resume: DeepWiki parity loop

The loop was set up but **paused before FIRST ACTION** because this environment's
egress proxy allowlist did not include `deepwiki.com` (every request returned
`403 host_not_allowed`). Once `deepwiki.com` is on the allowlist and the session
restarts, re-bootstrap (a fresh container loses `node_modules`, the Playwright
browser, and the editable Python install — but NOT these committed files), then
run FIRST ACTION.

## 1. Re-bootstrap (fresh container only)

```bash
# Python package (note: use the SAME interpreter as `python`, not /usr/bin/pip)
python -m pip install -e . --ignore-installed PyJWT

# Web deps + browser
cd web && npm install && npx playwright install chromium && cd ..
```

## 2. Confirm the allowlist now permits DeepWiki

```bash
curl -skS -o /dev/null -w "%{http_code}\n" https://deepwiki.com/   # want 200, not 403
cd web && node loop/observe.mjs --selfcheck                         # must exit 0
```

If this still 403s, the allowlist change did not take effect — stop and re-check
the environment's network policy (https://code.claude.com/docs/en/claude-code-on-the-web).

## 3. Build one real pool entry (no LLM needed)

Index CodeMiner itself in sparse/BM25 mode (dogfood) so `GET /api/repos` is
non-empty. (Bootstrap script to be added: `scripts/build_local_wiki_index.py`,
or reuse `IndexCompiler.compile_repo()` on the repo root and write a one-entry
`qa_registry.json`.) Optionally add a small `codeminer-base-dataset` instance via
`python scripts/build_qa_index.py --instances <id>`.

## 4. Start the stack

```bash
CODEMINER_DEMO_MODEL=anthropic/claude-... codeminer-web &   # backend :8000 (LLM only needed for Ask mode)
cd web && APP_BASE=http://127.0.0.1:3000 npm run dev &      # frontend :3000
```

## 5. Run FIRST ACTION

Follow `web/LOOP.md` → FIRST ACTION: observe deepwiki.com, write
`web/loop/OBSERVATIONS.md`, then `web/loop/TODO.md` (≥30 grounded items). Do not
start THE LOOP until TODO.md has ≥30 items. Then iterate under gates G1–G5 with
SELF-CRITIQUE every 5 items.

## State at pause

- Done: web deps, Chromium, harness (`web/LOOP.md`), scripts (`observe/shoot/diff/
  assert/checks.mjs`), TLS-interception worked around (`ignoreHTTPSErrors`).
- Pending: Python editable install (was still running), real index, FIRST ACTION.
- LLM: only `ANTHROPIC_BASE_URL` set; Ask mode needs working creds. Wiki content
  can be rendered from deterministic graph/chunk data (mock generation) without an
  LLM, so UI parity is not blocked on LLM creds.
