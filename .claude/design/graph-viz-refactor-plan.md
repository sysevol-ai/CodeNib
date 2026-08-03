# CodeNib Wiki — Graph Visualization Refactor Plan

> Recovered 2026-06-03 after a session crash. This is the execution plan behind
> PR #205 / branch `feat/graph-clustering-nav`. Phase 1 ("Files" layout) is
> already in progress (commit `9bf4a19`). Companion direction memo:
> [`graph-frontend-direction.md`](graph-frontend-direction.md).

> Hand this to Claude Code as a planning doc. Phases 0–2 are the core (they fix the
> "graph position doesn't align with code location" problem). 3–5 are follow-ups.
> **Before editing: discover the actual codebase** — locate the wiki frontend package,
> the subsystem-map component, the existing **Cytoscape.js** setup (instance init, stylesheet,
> registered layout/extension plugins), the FastAPI graph endpoint, and the igraph layer.
> Confirm the assumptions below against reality and adapt names/paths.
>
> **Renderer is fixed: stay on Cytoscape.js. Do not migrate to React Flow.** For this design
> (compound containers, multi-layout, expand/collapse, data-driven encoding) Cytoscape is the
> better-suited tool; migrating would mean rewriting the whole CodeGraph layer, dropping shipped
> features, and re-validating — for a benefit (rich React-component nodes) this design doesn't need.

## Assumptions (verify, then correct in-repo)

- Frontend: React + TypeScript + **Cytoscape.js** for the graph canvas (the `Fit` button =
  `cy.fit()`; the current `Subsystem map` is a Cytoscape instance). Recommended extensions:
  `cytoscape-fcose`, `cytoscape-dagre`, `cytoscape-expand-collapse`, optionally
  `cytoscape-node-html-label` if exact HTML pill styling is wanted.
- Backend: FastAPI graph server + **igraph** + DuckDB. Communities/metrics computed server-side.
- Current UI: each wiki section (Overview, Semantic Analysis, Linting Engine, …) renders one
  "Subsystem map" (~20–50 nodes). Controls: `Flow` / `Clusters` / `Fit`. Encoding today:
  node size = reference count, color = cluster, dashed border = external.
- Each left-nav section ≈ one community/cluster. Keep this — cluster lives at the **section**
  level, not as in-graph color (see Phase 1 rationale).

## Non-goals / guardrails (do NOT do these)

- **Do not switch renderers (no Sigma.js/WebGL, no React Flow).** Every view is bounded to
  dozens of nodes by design (focus+context + per-subsystem scoping), so Cytoscape.js is correct
  and already gives you compound nodes, a layout ecosystem, and expand/collapse. A WebGL renderer
  only wins at 1000+ nodes, which this design never hits.
- **Do not render the full global graph in one canvas.** Overviews use collapsed community
  super-nodes (Phase 5), never raw symbols.
- **Do not permanently split the wiki page into IDE panes.** Prose is the primary content;
  the code view is a transient slide-over (Phase 2).
- **One channel = one meaning.** Never encode two variables in the same visual channel.

---

## Phase 0 — Data contract + adapter (foundation)

Goal: backend ships a single typed payload per view; client is a dumb, fast renderer.
Compute communities, metrics, and roles **server-side (igraph)**. Compute layout coordinates
client-side as pure functions (Phase 1), so layouts stay reproducible and toggling is instant.

### Payload shape (TypeScript)

```ts
type LayoutMode = "files" | "flow" | "clusters";
type NodeKind = "function" | "method" | "class" | "interface" | "field" | "module";
type EdgeType = "CALLS" | "IMPORTS" | "EXTENDS" | "IMPLEMENTS" | "CONTAINS" | "REFERENCES";

interface GraphView {
  viewId: string;
  scope: "subsystem" | "overview" | "focus";
  proseAnchor?: string;          // wiki section/heading id this map backs (for Phase 3)
  nodes: GNode[];
  edges: GEdge[];
  files: FileGroup[];            // grouping for "files" layout
  communities: Community[];      // grouping for "clusters" layout + section mapping
}

interface GNode {
  id: string;
  label: string;
  kind: NodeKind;
  file: string;                  // e.g. "model.rs"
  startLine: number;             // from SCIP/LSP range — drives intra-file ordering
  endLine: number;
  communityId: string;
  external: boolean;             // outside this subsystem
  metrics: { pagerank: number; refCount: number; entryScore: number };
  proseAnchor?: string;          // heading/paragraph id discussing this symbol (Phase 3)
}

interface GEdge {
  id: string;
  source: string;
  target: string;
  type: EdgeType;                // "REFERENCE" by default until role-derivation lands (Phase 4b)
  weight: number;                // occurrence / reference count (anchor sites) — drives edge width
}
// NOTE: edges are SCIP/compiler-precise, so there is NO edge `confidence` (that is a
// heuristic-resolution artifact, e.g. GitNexus). Retrieval scores (BM25/vector/reranker) are a
// query-time property of *results*, not of static graph edges — keep them separate.

interface FileGroup { file: string; path: string; nodeIds: string[]; }
interface Community { id: string; label: string; nodeIds: string[]; sectionSlug?: string; }
```

### Backend tasks
- Extend the graph endpoint to emit the above. `pagerank` and `communityId` via igraph
  (`pagerank()`, Leiden/`community_leiden`). `startLine`/`endLine` from existing SCIP ranges.
- `weight` on edges = reference/occurrence count from SCIP (number of anchor sites) — surface it.
  Do not invent an edge `confidence`; SCIP edges are exact. Emit `type: "REFERENCE"` for all edges
  for now (fine-grained types are Phase 4b).
- Derive `external` relative to the requested subsystem scope.

### Frontend tasks
- Add the types above. Write `adaptGraphView(payload): cytoscape.ElementDefinition[]` producing
  Cytoscape elements:
  - symbol node: `{ data: { id, label, kind, file, startLine, endLine, communityId, external,
    pagerank, refCount, entryScore, parent: <fileNodeId | undefined> } }`
  - file group (compound parent): `{ data: { id: "file:<name>", label, isFile: true } }`
    (no position — Cytoscape auto-sizes the box around its children)
  - edge: `{ data: { id, source, target, type, confidence } }`
- Define one data-driven **Cytoscape stylesheet** with `mapData()` mappers and selectors
  (`node`, `node[?external]`, `:parent`, `edge`, and `.focus`/`.dimmed` classes for Phase 3/4).
  All visual encoding lives here, not in per-node code.

**Acceptance:** one fetch returns a `GraphView`; current map still renders unchanged through
the adapter (no visual regression yet).

---

## Phase 1 — "Files" layout mode (the core fix)

Goal: a layout where **position = code location**. Add `Files` next to `Flow`/`Clusters`,
and make it the **default** for subsystem maps.

### Layout (Cytoscape)
Files mode uses **compound nodes** (each file = a parent, symbols = children) with a
**deterministic `preset` layout** so position is exactly file + line:
- Children carry `data.parent = "file:<name>"`. Cytoscape auto-draws the bounding box and label
  around the children — that box *is* the file container, for free.
- `computeFilesPositions(view): Record<id, {x,y}>` — a pure function: lay file boxes in a grid
  (column-packed by box height); inside each file, stack children **sorted by `startLine`**
  top→bottom at a fixed row pitch. Feed via `cy.layout({ name: "preset", positions, fit: true })`.
  Preset (not fcose) for Files mode → fully deterministic, position literally tracks line number.
- Optional faint `L<startLine>` gutter shown via node label or `node-html-label`.
- Edges drawn normally between boxes. Cross-file edges will be longer — accepted cost of trading
  position for code-alignment; do not try to eliminate it.

### Visual encoding in Files mode (now that position = file, free up color)
Driven entirely by the stylesheet:
```js
// symbol nodes
{ selector: "node[!isFile]", style: {
    "shape": "round-rectangle",
    "width":  "mapData(pagerank, 0, 1, 20, 64)",   // size = importance → visual tension
    "height": "mapData(pagerank, 0, 1, 16, 40)",
    "background-color": "data(kindColor)",          // color = node kind (set in adapter)
    "label": "data(label)" } },
{ selector: "node[?external]", style: {             // external = dashed + dim
    "border-style": "dashed", "opacity": 0.55 } },
// file containers
{ selector: ":parent", style: {
    "background-opacity": 0.06, "border-width": 1, "border-color": "#cbd5e1",
    "shape": "round-rectangle", "text-valign": "top", "text-halign": "center",
    "label": "data(label)", "font-family": "monospace" } },
// edges  (width = reference frequency; opacity is NOT data-bound — reserved for .dimmed/.focus)
{ selector: "edge", style: {
    "width": "mapData(weight, 1, 20, 1, 4)",
    "curve-style": "bezier", "target-arrow-shape": "triangle" } },
// interaction states (Phase 3/4)
{ selector: ".focus",  style: { "border-color": "#2563eb", "border-width": 3, "z-index": 10 } },
{ selector: ".dimmed", style: { "opacity": 0.12 } },
```
Map `kind → kindColor` in the adapter (function/class/method/field/interface). Reserve the
accent (`.focus`) for the focused node + its neighborhood.

### Rationale for cluster handling
Within a single subsystem map, the section already *is* the community, so cluster-as-color is
redundant here — spend color on `kind` instead. Cluster stays meaningful at the **overview**
level (Phase 5) and as the wiki's section structure. Keep `Clusters` mode available for users
who want the community-grouped force view; just don't make it the subsystem default.

### Tasks
- `computeFilesPositions` (preset, deterministic) as a pure, unit-tested function.
- One `applyLayout(cy, mode, view)` entry point that toggles between:
  - `files`   → `preset` with `computeFilesPositions` (compound boxes on).
  - `flow`    → `cytoscape-dagre` (`rankDir: "TB"` or `"LR"`) so call direction reads as a flow.
    Note: dagre ignores compound boxes — flatten file parents in Flow mode (call topology, not
    file grouping, is the point here).
  - `clusters`→ `cytoscape-fcose` (handles compound + community grouping; seed by `communityId`).
  Toggling re-runs only the layout, never refetches.
- Make `files` the **default** for subsystem maps; keep `Flow`/`Clusters` as toggles. Put the
  default behind a feature flag so Files-vs-Flow can be A/B'd.

**Acceptance:** in Files mode, every node sits inside its file box, ordered by line; same file →
same box; toggling Flow/Clusters/Files is instant and deterministic; `Fit` still works.

---

## Phase 2 — Linked code view (slide-over drawer)

Goal: recover graph↔source mapping through interaction, without eating page space.

- Click (or hover-intent) a node → **right slide-over drawer** (overlay, does not reflow the
  page). Contents: the symbol's source snippet (fetch by `file` + `startLine..endLine` ± a few
  lines of context), its 1-hop neighbors (in/out, grouped by edge type), and an `open ↗`.
- Trigger via `cy.on("tap", "node[!isFile]", evt => openDrawer(evt.target.id()))`. The drawer is
  a plain React component outside the canvas (renderer-agnostic).
- `open ↗` → navigate to a dedicated symbol/file page (focus Cytoscape view + full source +
  Ask). This is the heavy "dig in" path — must not be the only path.
- Esc / click-outside closes. Keep it non-destructive and fast.
- Add a backend snippet endpoint: `GET /snippet?file=&start=&end=` returning the code range
  (+ a few context lines). Do not ship whole files to the drawer.

**Acceptance:** clicking any node opens a snippet drawer in <300ms without reflowing prose;
`open ↗` navigates to the deep view; Esc closes.

---

## Phase 3 — Graph ↔ prose linking

Goal: deliver on "this page is a view over the graph" — make it bidirectional and spatial.

- Node click also scrolls the prose to `node.proseAnchor` and highlights that paragraph.
- `IntersectionObserver` on section headings → as the reader scrolls into "Type Inference",
  highlight/focus the matching nodes (by `communityId`/`file`) and dim the rest.

**Acceptance:** scrolling prose visibly shifts node emphasis; clicking a node jumps+highlights
its paragraph.

---

## Phase 4 — Focus + context (degree-of-interest)

Goal: implement the focus paradigm so dense maps stay legible.

- Selecting a node enters focus mode: compute `n = node.closedNeighborhood()` (or
  `.successors()`/`.predecessors()` for directed call context), add `.focus` to it and `.dimmed`
  to `cy.elements().difference(n)` inside a `cy.batch()`. Esc / "clear focus" removes the classes.
- Collapsed nodes expand on demand via `cytoscape-expand-collapse` (click to reveal further hops).

**Acceptance:** focus dims context and highlights the n-hop neighborhood; clearing restores.

---

## Phase 4b — Fine-grained edge types (optional, deterministic from SCIP)

Not required for the core; do it as a near fast-follow because it upgrades Flow-mode legibility
and the per-edge-semantic layout. **No confidence/heuristics** — all derivations are SCIP-exact.

**Scoping check first (~30 min):** does the SCIP ingest still retain occurrence `symbol_roles`,
symbol `kind`, and `SymbolInformation.relationships`? If yes → this is a light mapping pass. If
those were collapsed to a generic reference at parse time → it's a heavier ingest change; defer.

Derivation (all deterministic):
- `IMPORTS` ← occurrence with the Import role.
- `INHERITS` (extends/implements) ← `relationships[].is_implementation` / type-hierarchy rels.
  SCIP doesn't cleanly split extends vs implements — merge into one `INHERITS` type.
- `CALLS` ← reference occurrence whose target symbol `kind ∈ {function, method}`.
- `REFERENCE` ← everything else (default).
- Containment (file→class→method) is NOT an edge here — it's the compound `parent` nesting.

Once available:
- Flow mode (`cytoscape-dagre`) lays out **only `CALLS`** as the DAG; render `IMPORTS`/`INHERITS`
  as weak/secondary styling so they don't pollute the call flow.
- Stylesheet: `edge[type='CALLS']` solid, `edge[type='IMPORTS']` dashed/dim,
  `edge[type='INHERITS']` distinct color + hollow arrow.

**Acceptance:** edges carry a real `type`; Flow mode reads as a clean call DAG with import/
inheritance edges visually subordinated.

---

## Phase 5 — Cross-subsystem overview (collapsible community super-nodes)

Only if a cross-subsystem overview is wanted as a product feature.

- Model communities as **compound parents** (`data.parent = "community:<id>"`) and use
  `cytoscape-expand-collapse` to collapse each into a super-node sized by member count; expanding
  drills into file→symbol. This is nearly native in Cytoscape — a strong reason the renderer stays.
- Three-level collapsible hierarchy: community → file → symbol. This is also the correct way to
  render any "whole repo" view — never raw symbols on one canvas.

**Acceptance:** overview renders dozens of community super-nodes; expanding one drills into its
files/symbols using the Phase 1 Files layout.

---

## Suggested order & sizing
- **Now (core):** Phase 0 → 1 → 2. This is what removes the headache.
- **Next:** Phase 3 (cheap, high polish), Phase 4 (interaction depth).
- **Later:** Phase 5 only if you ship a global overview.

## Quick reference — visual channel assignments (subsystem / Files mode)
| Channel | Encodes |
|---|---|
| position | file + startLine (code location) |
| color | node kind |
| size | pagerank / importance |
| edge width | reference frequency (anchor / occurrence count) |
| edge opacity | reserved for focus/context dimming — not a data channel |
| edge color/style | edge type once Phase 4b lands (else single neutral style) |
| dashed + desaturated | external |
| accent | focus + neighborhood |

---

## Status & reality reconciliation (2026-06-03)

> Audited the plan against the live PR #205 code (13-agent reconcile, file:line
> verified). **Read this before implementing any phase** — the plan above
> describes an idealized `GraphView` contract that does **not** exist in the
> code; the real shape is `CodemapResponse` / `CodemapNode` / `CodemapEdge`
> (`web/lib/api.ts:131-172`). Decision pending: ratify `CodemapResponse` and
> amend the plan, vs. rename to `GraphView` (see "Open decision" below).

| Phase | Status | ~% | Where |
|---|---|---|---|
| 0 — Data contract + adapter + stylesheet | **done** | 90 | **2026-06-03:** pure `adaptGraphView()` + `computeFilesPositions()` extracted (`web/lib/filesLayout.ts`, unit-tested); `edge.weight`=anchor count wired to `mapData(weight)` width; `ref_count`/`entry_score` added in `codemap.py _enrich()`. Contract **ratified as `CodemapResponse`** (no rename). Deferred tail: promoting ephemeral file boxes into the serializable payload (pairs with Phase 5). |
| 1 — "Files" layout (core fix) | **done** | 100 | `CodeGraph.tsx` `runFilesLayout()` applies the now-pure `computeFilesPositions()`: compound file boxes, children sorted by line, grid-packed, preset layout; default `useState('files')`; instant toggle no refetch. **2026-06-03:** packing math unit-tested; edge width now `mapData(weight)`. |
| 2 — Linked code-view drawer | **partial** | 50 | source peek works (`GraphView.tsx`→`/api/.../source`) but **inline, reflows page** (`.callsite-peek` margin not fixed), **no Esc/click-outside**, no 1-hop neighbors, no `open ↗` deep-link |
| 3 — Graph ↔ prose linking | **not started** | 10 | no `proseAnchor` field anywhere; one-way `IntersectionObserver` (`page.tsx:155-167`) is reusable scaffolding only |
| 4 — Focus + context (DOI) | **partial** | 30 | hover-only DOI (`CodeGraph.tsx:363-378` `.hl`/`.faded`); **no persistent click-to-focus, no Esc clear**; `cytoscape-expand-collapse` not installed |
| 4b — Fine-grained edge types | **not started** | 0 | edges carry only `source/target/anchors`; no `type`/`weight`. **Risk:** `code_graph.py:51-52,68-69` stores no edge type and doesn't expose `SymbolInformation.relationships` → INHERITS may need a graph-layer ingest change, not just `codemap.py` |
| 5 — Cross-subsystem overview | **not started** | 0 | needs `cytoscape-expand-collapse` + `Community[]` metadata (depends on Phase 0 + 3) |

**Confirmed good:** there is **no edge `confidence` field** — edges are SCIP/LSP-exact call sites with file+line anchors. The plan's "no confidence" guardrail holds; the compiler-precise edge-provenance differentiator is intact.

### Corrected assumptions (plan said → reality)
- **`GraphView`/`GNode`/`GEdge`/`FileGroup`/`Community` types + pure `adaptGraphView()`** → none exist. Backend returns `CodemapResponse {available, root, root_label, direction, depth, truncated, nodes, edges, mermaid, note}`; element construction is inlined in `CodeGraph.tsx:203-243`.
- **`metrics:{pagerank,refCount,entryScore}`** → only a flat `importance` (PageRank percentile) + `community` id. No `refCount`, no `entryScore`. On the Phase 0 critical path for 3 & 4.
- **edges carry `type` + `weight`; width via `mapData(weight)`** → edges carry only `source/target/anchors`; width hardcoded 1.5px. `anchors.length` implicitly = weight but isn't surfaced.
- **Phase 2 = fixed overlay w/ neighbors + deep-link + Esc** → inline (reflows), source-only, `Focus here` only, ×-button-only close.
- **left-nav section ≈ one community; `proseAnchor` links them** → wiki sections are `WikiPageRef` title trees; communities are Leiden clusters on the REFERENCE subgraph; no mapping, no `proseAnchor`.
- **file boxes are data** → ephemeral, `id=`filebox::${file}``, rebuilt every toggle (`CodeGraph.tsx:98`), not serializable. Will collide with Phase 5 community compounds unless grouping is unified in the data layer first.

### Decision (2026-06-03): ratified `CodemapResponse`
Resolved in favour of keeping `CodemapResponse` / `CodemapNode` / `CodemapEdge` as
the contract — **no rename** (no behavioral payoff). The plan's idealized
`GraphView` / `GNode` / `GEdge` names earlier in this doc are **aspirational
only**; implement against `CodemapResponse`, now extended with `edge.weight` and
node `ref_count` / `entry_score`. The pure adapter seam is `adaptGraphView()` in
`web/components/CodeGraph.tsx`; the pure Files-layout math is
`computeFilesPositions()` in `web/lib/filesLayout.ts`.

### Recommended next step — Phase 0 DONE (2026-06-03)
Phase 0 hardening landed and is verified (`tsc --noEmit`, `next build`, `vitest`
4/4, Python black/isort/flake8, backend functional check):
- pure `adaptGraphView()` extracted out of the render effect;
- pure, unit-tested `computeFilesPositions()` (`web/lib/filesLayout.ts`) — also
  closes Phase 1's missing regression guard;
- `edge.weight` = anchor count (`codemap.py _enrich()` → `api.ts` →
  `mapData(weight,...)` edge width, replacing the hardcoded 1.5px);
- `ref_count` (in-degree) + `entry_score` (out/(in+out)) in `codemap.py _score()`
  for downstream Phase 3/4 weighting.

**Next target:** finish **Phase 2** (overlay positioning so the peek stops
reflowing the page + Esc/click-outside close + 1-hop neighbors + `open ↗`) — its
Esc handler pairs naturally with Phase 4's focus-clear. Do Phase 4b `edge.type`
before the "neighbors grouped by edge type" part of Phase 2. Still-deferred Phase
0 tail: promote the ephemeral `filebox::` boxes into the serializable payload —
do it alongside Phase 5 community compounds to avoid two ad-hoc compound schemes.

---

## PRODUCT PIVOT — differentiate the two graph surfaces (2026-06-03, Pass 1)

User direction after seeing the demo: the **top-down dependency** reading is the
thing to showcase; the wiki subsystem map and the standalone Graph view were
**homogeneous (same component, same 3-mode toggle)** and must be **differentiated**.

**New model (both render through the shared `CodeGraph`, now keyed by a `variant` prop):**
- **Wiki subsystem map** (`variant="wiki"`, `page.tsx`) = a single, focused,
  **top-down dependency-ordered** view. **No Flow/Clusters toggle** — a static
  "↓ top-down dependencies" label instead. For *reading* structure inline with prose.
- **Standalone Graph view** (`variant="explore"`, `Codemap` → `GraphView`) =
  **exploration**: keeps the Files/Flow/Clusters toggle (the "playground").

**Phase 1 layout semantics CHANGED (important):** the "Files" layout's vertical
axis is no longer file+line packing — it now encodes **dependency depth**.
`computeFilesPositions(items, edges, cfg)` builds a file-level DAG and lays files
in horizontal bands by longest-path layer (callers/entry top → callees/leaves
bottom), symbols by line within each file box; size-packed fallback when there are
no cross-file edges. Pure + unit-tested (`web/lib/filesLayout.test.ts`).

**Phase 4 (focus + context) — partially landed (the "click→all-grey" fix):** the
old `.faded {opacity:0.25}` wash is replaced. Focused node gets a filled accent
(`.focus`) + its neighborhood stays full-opacity (`.hl`); context dims to a
legible `0.45`. Click = persistent focus (degree-of-interest); Esc / background
click clears; hover preview is suppressed while focused so it can't clobber.

Verified: `tsc --noEmit`, `next build`, `vitest` 6/6, live screenshots of both
surfaces + a focused state (no console/page errors).

**Pass 2 (pending) — explore-only power features the user also selected:**
click-to-expand neighbors (`cytoscape-expand-collapse`) + symbol search/filter.
**Refinements noted from the live screenshots:** isolated/no-edge files clump in
the top band (funnel) — sink them lower/aside; top-band horizontal crowding at
`COL_W=250` for wide labels; dimmed-context legibility could be nudged.

### Pass 1b (2026-06-03) — overlap, focus-grey, and "one view" feedback

User feedback after Pass 1: (1) nodes/file-boxes still overlap; (2) clicking a
node throws a **grey cover** over everything (can't read content) — the pop was
fine but the grey wasn't; (3) the three modes (Files/Flow/Clusters) are
**redundant** — keep only one view. All three addressed:

- **Overlap fixed:** `computeFilesPositions(items, edges)` now spaces lanes by each
  box's **measured width** (`item.width` = `node.outerWidth()` fed from CodeGraph),
  so wide boxes never overlap their neighbour; and **isolated files** (no
  cross-file edge) sink to a dedicated **bottom band** instead of crowding the
  entry row. Gaps bumped. Two new unit tests (8/8 total).
- **No more grey cover:** focus/hover **never dim nodes** now — every node stays
  fully legible. Only the *unrelated edges* fade (`edge.faded`); the focused node
  gets the filled accent (`.focus`) and its neighbourhood the `.hl` border. The
  `node.faded` style was deleted.
- **Single view:** the Files/Flow/Clusters toggle is **gone from both surfaces**;
  the `dagre`/`fcose` imports, `ensureLayouts`, `layoutMode` state, the
  flow/clusters layout config, and the dead community-colour palette
  (`FILE_COLORS`/`colorFor`/`commColor`) were all removed (CodeGraph.tsx −39 net).
  Both variants render the one top-down dependency view; `variant` now only gates
  Pass-2 explore extras. (`cytoscape-dagre`/`-fcose` still in package.json — unused
  now, optional to uninstall.)

Verified: `tsc` 0, `vitest` 8/8, `next build` OK, live screenshots of all three
states (no overlap, legible focus, single-view explore) with no page errors.

### Pass 1c (2026-06-03) — vertical overlap + unify graph into a modal

More feedback after Pass 1b: (1) **vertical** overlap remained (left-right was
fixed, but top-bottom nodes/boxes still overlapped); (2) the separate Graph
*view* is redundant now that the wiki subsystem map is good — **unify** graph
access into **one CodeGraph button next to the repo name**, opening an
**independent full-screen modal** to explore.

- **Vertical overlap fixed:** `computeFilesPositions` now stacks symbols and
  sizes boxes/bands by each node's **measured height** (`item.height` =
  `node.outerHeight()`), mirroring the width fix — so multi-line/wrapped labels
  and tall boxes no longer overlap the row below or the next dependency band.
  Both axes are now measurement-driven. (8/8 tests, incl. width-spacing.)
- **Graph access unified into a modal:** removed the **Wiki/Graph mode tabs** and
  the `mode` state from `page.tsx`. Added a **⌗ CodeGraph** button in the
  breadcrumb next to `repo.repo` (shown when `capabilities.codemap`), opening a
  **full-screen modal** (`.graph-modal*`) that renders the explore `Codemap`
  (Esc / click-scrim / × to close). The wiki subsystem map's "Focus here" now
  opens that modal seeded on the symbol (was: switch to codemap mode). The
  inline `mode === "codemap"` render and its rail note were removed.

Verified: `tsc` 0, `vitest` 8/8, `next build` OK; live screenshots — wiki map
(no vertical overlap), header CodeGraph button, and the open modal — no page
errors. Stale-but-harmless: `.wiki-modeswitch`/`.mode-tab` CSS now unused;
`cytoscape-dagre`/`-fcose` still in package.json.
