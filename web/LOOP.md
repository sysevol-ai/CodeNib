<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# DeepWiki Parity Loop — Harness

This is the operating spec for the self-improving loop that builds CodeMiner's
DeepWiki clone to parity with https://deepwiki.com. Read this file, then execute
the **FIRST ACTION** section before anything else. Do not skip ahead.

> Kickoff prompt (fresh session): "Read CLAUDE.md / web/LOOP.md. Execute the
> 'FIRST ACTION' section now. Do not start the loop until you have produced a
> web/loop/TODO.md with ≥30 items based on actual DeepWiki observation."

## MISSION

Reach **structural/visual parity** with DeepWiki for three surfaces:
- **(A) Landing / repo pool** — the page that lists indexable repositories.
- **(B) Repo wiki page** — left-rail TOC + content with syntax-highlighted,
  source-grounded code references + cross-symbol links + at least one diagram.
- **(C) Display-mode switch** — Wiki / Diagram / Ask. "Ask" reuses the existing
  chat verbatim; "Diagram" renders Mermaid.

Parity is judged on **layout / chrome / typography / component structure**, not
literal content identity (our repos differ from DeepWiki's). See DIFF SEMANTICS.

## ARTIFACTS (all under `web/loop/`)

| File | Role |
|---|---|
| `OBSERVATIONS.md` | What was actually seen on deepwiki.com (never assumed). |
| `TODO.md` | The ≥30-item checklist — the contract. Each item is checkable. |
| `CRITIQUE.md` | Running log of every SELF-CRITIQUE round + items it spawned. |
| `reference/*.png` | DeepWiki screenshots (the baselines). |
| `app/*.png` | Screenshots of OUR running app, per item. |
| `diffs/*.png` + `diff-report.json` | Diff overlays + numeric metrics. |
| `observe.mjs` | Playwright: capture deepwiki.com views + DOM/style dumps. |
| `shoot.mjs` | Playwright: capture our app by URL + optional selector clip. |
| `diff.mjs` | pixelmatch/pngjs region diff → mismatch ratio. |
| `assert.mjs` | Playwright DOM/behavior assertions for a named check. |

## FIRST ACTION (run once — gated by G1)

1. **Confirm the browser works.** `node web/loop/observe.mjs --selfcheck` must
   launch Chromium and reach deepwiki.com. If it cannot reach the site, STOP and
   report — the loop cannot do live visual diff (do not fall back to imagining).
2. **Observe the reference.** Capture full-page + key-region screenshots of:
   landing/repo pool, a repo wiki page chrome, the left TOC, a content section
   with code highlighting, a diagram, a citation/source view, and Ask mode. Save
   to `reference/`. Dump DOM structure + computed styles (font family/size,
   colors, spacing, max-width) for each major region.
3. **Write `OBSERVATIONS.md`** — enumerate components, layout, color/typography
   tokens, and interactions actually observed. Cite the screenshot for each.
4. **Derive `TODO.md` with ≥30 objectively-checkable items.** Each item states:
   `[ ] (view) acceptance check — verified by: <diff metric & threshold | DOM/behavior assertion>`.
   Group by surface (A/B/C). Examples of good items:
   - `[ ] (A) search box is centered above a responsive grid of repo cards; verified by: assert.mjs grid has ≥1 .repo-card and a top search input`
   - `[ ] (B) left TOC is sticky on scroll and highlights the active section; verified by: assert.mjs scroll → active item aria-current`
   - `[ ] (B) fenced code blocks are syntax-highlighted with token colors + a copy button; verified by: assert.mjs code has >1 colored token span + copy button`
   - `[ ] (C) mode switch shows Wiki/Diagram/Ask tabs; active tab underlined; verified by: assert.mjs 3 tabs, one [data-active]`
   **Do not proceed to THE LOOP until `TODO.md` has ≥30 reference-grounded items.**

## THE LOOP (repeat until EXIT CRITERIA)

1. Pick the **next unchecked** `TODO.md` item.
2. Implement the **smallest** change that satisfies it (frontend in `web/`,
   backend in `codeminer/web/`, generation in `codeminer/wiki/`).
3. Reload the running app. Capture evidence:
   - `node web/loop/shoot.mjs <route> <outName> [selector]` → `app/<outName>.png`
   - For visual items: `node web/loop/diff.mjs reference/<ref>.png app/<outName>.png <outName>`
     → appends `{item, metric, threshold, pass}` to `diff-report.json`.
   - For behavior items: `node web/loop/assert.mjs <checkName>` → pass/fail JSON.
4. **Fail → stay on this item and iterate.** Record the attempt. Do NOT check off.
5. **Pass → check the item off**, citing the screenshot path + metric/assertion.
6. After every **5** newly-checked items (and on any G5 trigger) → run SELF-CRITIQUE.

## GATES — hard rules. A violation is a bug, not a judgment call.

- **G1 — Observation-first.** No implementation until `OBSERVATIONS.md` and a
  ≥30-item `TODO.md` exist and are grounded in `reference/` screenshots.
- **G2 — Evidence-required.** An item is checked off ONLY with (a) an `app/`
  screenshot + a `diff-report.json` metric below its threshold, or (b) a passing
  `assert.mjs` result. Never check off from reasoning alone.
- **G3 — No-imagined-rendering.** Every visual claim must come from a screenshot
  of the *running* app. Never assert what JSX/CSS "would" render without a shot.
- **G4 — Regression-guard.** Before checking off, re-run a rotating sample of
  previously-passed `assert.mjs` checks. A regression reopens that item.
- **G5 — Anti-rationalization.** You may NOT loosen, delete, reword, or relax the
  threshold of any acceptance criterion to make it pass. If an item is genuinely
  wrong or over-broad, you must ADD a stricter sub-item and LEAVE the original,
  then immediately trigger a SELF-CRITIQUE. Any urge to widen criteria is itself a
  trigger. Editing `TODO.md` to make red turn green (other than checking a box
  whose evidence exists, or adding stricter items) is forbidden.

## SELF-CRITIQUE (every 5 checked items + on any G5 trigger)

1. Take FRESH screenshots of ALL major surfaces (A/B/C) of the running app.
2. Compare to `reference/`. List **≥3 concrete discrepancies** (with screenshot
   refs). A "no issues found" critique is itself a G5 violation — redo it; the
   critique's job is to find defects, not to bless progress.
3. Append the discrepancies to `TODO.md` as new items and log the round in
   `CRITIQUE.md` (timestamp, items reviewed, new items spawned).

## DIFF SEMANTICS

Our content differs from DeepWiki's, so diffs target layout/chrome/typography/
component structure — not pixel-identical content:
- Compare **equivalent regions** (header, TOC rail, card grid, code-block styling)
  using clipped screenshots of matching selectors, not whole pages.
- Prefer `assert.mjs` DOM-presence + computed-style assertions for structure.
- Reserve `diff.mjs` pixel diff for chrome / skeleton / empty states where layout
  is comparable. Each item declares its own threshold in `TODO.md`.

## EXIT CRITERIA

All `TODO.md` items checked **AND** two consecutive SELF-CRITIQUE rounds produce
zero new *high-severity* items. Then emit a **final parity report**: side-by-side
`reference/` vs `app/` screenshots per surface + the diff metrics table.

## ANTI-PATTERNS (explicitly forbidden)

Imagining HTML; widening/deleting criteria; batch-checking items without per-item
evidence; skipping observation; stopping because it "feels done"; hammering
deepwiki.com (observe a few pages, cache the baselines).
