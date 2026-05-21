# DeepWiki-style site for CodeMiner — Build TODO

Environment notes (verified 2026-05-21):
- Toolchain OK: node 22, npm, Playwright 1.56.1 + chromium build 1194 (launch + screenshot confirmed).
- No `/references/*.png` exist in this sandbox, so acceptance is verified against
  written criteria via DOM inspection + screenshot vision (not pixel diff).
- Wiki content is generated **offline** from the real source via Python `ast`
  (`web/scripts/gen_wiki.py` -> `web/data/wiki.json`); no network/LLM needed.
- Live "Ask a question" answers need an LLM key + a built index (tree-sitter
  parser download is blocked here), so that feature degrades gracefully and is
  verified for UI presence/behavior, not live answers.

Verify each item with: `node verify.mjs <url> <id> [w] [h]` (writes
`/verification/<id>.png` + prints console/pageerror JSON).

---

- [✅] [P0-DATA] Generate wiki data from source
  Criteria:
    - `gen_wiki.py` parses every `codeminer/**/*.py` with `ast` (no crashes).
    - `wiki.json` has: repo meta (name, languages, license, commit, commit date,
      file_count, loc), >=12 modules, per-file symbol outlines w/ docstrings,
      curated pages, and an architecture mermaid string.
    - Re-runnable + deterministic.
  Verification: run script, inspect wiki.json counts.
  Status notes: 16 modules, 146 files (0 parse errors), 280 symbols, 4 pages,
    mermaid string present; all repo meta keys present. Deterministic (sorted).

- [✅] [P1-LAYOUT] App shell: top bar + collapsible module-tree sidebar
  Criteria:
    - Top bar: repo name, primary language, license, last-commit (rel time),
      and a focusable "Ask a question" input.
    - Left sidebar lists wiki sections + a collapsible module tree (expand/collapse).
    - Dark theme consistent; sticky header; content scrolls independently.
    - Mobile (375px): sidebar collapses to a toggle; no horizontal scroll.
  Verification: screenshot `/` desktop + 375px; console clean.
  Status notes: home-desktop.png + home-mobile.png + home-mobile-nav.png.
    Ask input focusable (aria ok). Tree expand 0->7 files. Mobile toggle opens
    drawer (open 0->1). No horizontal scroll (375). Console clean.

- [✅] [P1-HOME] Homepage overview
  Criteria:
    - Hero shows repo name, one-line description, language/license/commit chips.
    - Mermaid architecture diagram renders (SVG present, no mermaid console errors).
    - Module overview grid: one card per module w/ name, blurb, file/symbol counts,
      linking to the module page.
    - Mobile (375px): no horizontal scroll.
  Verification: screenshot `/` 1280 + 375; assert `svg` in `.mermaid`; console clean.
  Status notes: hero + chips + supports line present. Mermaid SVG with 13 nodes.
    16 module cards linking to /modules/*. No horizontal scroll at 375.

- [✅] [P1-MODULE] Per-module page
  Criteria:
    - Heading = module title + path; description paragraph.
    - File table: file name, language, LOC, symbol count; rows link to file page.
    - Breadcrumb back to home; sidebar highlights active module.
  Verification: screenshot `/modules/agent`; click-through links resolve (200).
  Status notes: module-agent.png; 31 file rows, h1 "Agent", first file link -> 200.
    Console clean. Sidebar Agent active.

- [✅] [P1-FILE] Per-file page
  Criteria:
    - Header: file path, language, LOC, link to source on the repo host.
    - Module docstring rendered (if present).
    - Symbol outline: classes (with methods) + functions, each w/ signature line
      number and docstring; anchored list.
    - Breadcrumb home > module > file.
  Verification: screenshot `/files/codeminer/agent/runner.py`; symbols visible.
  Status notes: file-runner.png; AgentRunner class + __init__/run methods w/
    signatures + L46/L58/L109; docstring rendered; View source -> GitHub URL;
    breadcrumb Home/Agent/runner.py. Console clean.

- [✅] [P1-WIKI] Curated wiki pages (markdown + mermaid)
  Criteria:
    - Pages: Overview, Architecture, Retrieval Pipeline, Testing & CI.
    - Markdown renders (headings, lists, tables, code); mermaid blocks render.
    - Reachable from sidebar; active page highlighted.
  Verification: screenshot `/wiki/architecture`; console clean.
  Status notes: wiki-architecture.png (mermaid SVG + sections, sidebar active),
    wiki-testing.png (1 table/4 rows + code block). Console clean.

- [✅] [P1-ASK] "Ask a question" wired to backend
  Criteria:
    - Submitting a question POSTs to `/api/chat`; answer + citations render.
    - If backend unreachable, shows a clear inline notice (no uncaught errors).
  Verification: screenshot ask panel with backend down -> graceful notice; console clean.
  Status notes: ask-degraded.png; drawer opens, echoes question, shows clear
    orange notice on fetch failure, 0 pageErrors. Live answers need codeminer-web
    + index + LLM key (not available in sandbox) — UI/error path verified.

- [✅] [P2-POLISH] Build clean + no console noise + mobile pass
  Criteria:
    - `next build` completes with zero errors and zero warnings.
    - Home, module, file, wiki pages: zero console errors/warnings, zero pageerrors.
    - 375px: no horizontal scroll on home, module, file pages.
  Verification: build log + verify.mjs JSON across routes.
  Status notes: `next build` exit 0, "Compiled successfully", types pass, 170
    static pages, zero warnings. Final pass (final-*.png) over home/module/file/
    wiki at 1280 + 375: all 200, hscroll=False, 0 errs/warns/pageErrs.

- [✅] [P2-NAV] Disambiguate duplicate file names in sidebar
  Self-critique find: agent/skills/* have many identically-named __init__.py /
  executor.py, so the basename-only sidebar looked like duplicates.
  Criteria:
    - Sidebar file entries show enough path context to be unique within a module.
    - No horizontal overflow introduced; console clean.
  Verification: screenshot sidebar on /modules/agent.
  Status notes: nav-disambiguated.png; 31/31 unique labels
    (skills/bm25_search/executor.py etc.). Mobile hscroll=False, console clean.
