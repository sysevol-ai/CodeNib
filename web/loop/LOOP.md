# LOOP.md — DeepWiki-parity self-improving loop

This file is the **harness spec** the agent reads and executes. It encodes an
observation-driven loop that drives our app (`web/` + `codeminer/web/`) to
structural/visual parity with https://deepwiki.com. Build details for the data
model + agent-infra coupling live in GitHub issue **#166** (`codeminer/wiki/`).

> Prerequisite: `deepwiki.com` must be reachable from this environment.
> Verify with `node web/loop/observe.mjs --probe`. If it prints
> `host_not_allowed` / a cert error, STOP — the network allowlist does not
> permit DeepWiki (set Network access = Custom, add `deepwiki.com` +
> `*.deepwiki.com`, keep default package managers) and restart the session.

## MISSION

Reach parity with DeepWiki for three surfaces, driven entirely by observed
evidence (never by imagining markup):

1. **Repo-pool landing** — search + grid of repo cards.
2. **Repo wiki page** — left TOC (sticky/collapsible, active-section highlight) +
   source-grounded prose + syntax-highlighted code refs + cross-symbol links +
   at least one diagram.
3. **Display-mode switch** — Wiki / Diagram / Ask. *Ask = the existing chat,
   verbatim.*

## FIRST ACTION (run once, before any implementation — gated by G1)

1. `node web/loop/observe.mjs` — capture full-page + key-region screenshots of
   deepwiki.com (landing pool, a representative repo wiki page, TOC, a
   highlighted code section, a diagram, citation/source view, Ask mode) into
   `web/loop/reference/`, plus DOM structure + computed styles
   (`reference/*.json`) for the major regions.
2. Write `web/loop/OBSERVATIONS.md`: enumerate components, layout, color/type
   tokens, and interactions ACTUALLY seen. No assumptions.
3. Derive `web/loop/TODO.md` with **≥30** objectively-checkable items. Each item
   names: the view, the acceptance check, and how it is verified (a diff metric
   threshold OR a DOM/behavior assertion). **Do not start THE LOOP until ≥30
   grounded items exist.**

## THE LOOP (repeat until EXIT)

1. Pick the next unchecked TODO item.
2. Implement the smallest change that satisfies it (`web/`, and if needed
   `codeminer/web/` + `codeminer/wiki/` per issue #166).
3. Reload the running app; `shoot.mjs` screenshots the matching view;
   `diff.mjs` produces a numeric metric vs the matching reference region — OR
   run the item's DOM/behavior assertion via Playwright.
4. **Fail** → stay on the same item, record the attempt + metric, iterate.
   **Pass** → check it off, storing the screenshot path + metric/assertion
   result as evidence inline in `TODO.md`.
5. Every 5 checked items (and on any G5 trigger) → run **SELF-CRITIQUE**.

## GATES (hard rules — violations are bugs, not judgment calls)

- **G1 Observation-first.** No implementation until `OBSERVATIONS.md` +
  `TODO.md` (≥30) exist and are reference-grounded.
- **G2 Evidence-required.** Check an item off ONLY with (a) an app screenshot +
  a diff metric below its threshold, or (b) a passing DOM/behavior assertion.
  Never by reasoning alone.
- **G3 No-imagined-rendering.** Every visual claim comes from a screenshot of
  the RUNNING app — never from reading JSX/CSS and asserting what it "would"
  render.
- **G4 Regression-guard.** Before checking off, re-run a rotating sample of
  previously-passed assertions. A regression reopens that item.
- **G5 Anti-rationalization.** You may NOT loosen, delete, reword, or relax the
  threshold of any acceptance criterion to make it pass. If an item is
  genuinely wrong/over-broad, ADD a stricter sub-item and leave the original,
  then trigger a SELF-CRITIQUE. Any urge to widen criteria is itself a trigger.

## SELF-CRITIQUE (every 5 items + on any G5 trigger)

Take fresh screenshots of all major views, compare to reference, and list **≥3
concrete discrepancies**; append them as new TODO items. A "no issues found"
critique is itself a G5 violation and must be redone — the critique's job is to
find defects, not to bless progress.

## DIFF SEMANTICS

Our repos/content differ from DeepWiki's, so the metric targets
layout/chrome/typography/component structure, NOT literal content identity:
- Use DOM-presence + computed-style assertions for structure.
- Reserve pixel diff (`diff.mjs`) for chrome/skeleton states and equivalent
  regions (header, TOC rail, card grid, code-block styling).
- Thresholds are per-item in `TODO.md`.

## EXIT CRITERIA

All TODO items checked AND two consecutive SELF-CRITIQUEs produce 0 new
high-severity items. Emit a final parity report: side-by-side reference/app
screenshots per major view + the diff metrics.

## ANTI-PATTERNS (forbidden — these are the known failure modes)

Imagining HTML; widening or deleting criteria; batch-checking items without
per-item evidence; skipping observation; stopping because it "feels done."
