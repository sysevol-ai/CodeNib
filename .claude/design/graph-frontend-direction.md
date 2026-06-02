# Graph-frontend direction & differentiation

Design memo for the web demo's graph. Records the thesis and the concrete path.
Operational run/loop instructions live in
[`../guides/frontend-loop.md`](../guides/frontend-loop.md).

## Status — P1+P2 implemented (2026-06-01)

The graph overhaul and the click-to-source differentiator are **live** on branch
`worktree-frontend-loop-guide` (screenshots in `web/`'s sibling `verification/`):

- **Mermaid → Cytoscape** (`web/components/CodeGraph.tsx`): dagre LR layout,
  per-file colour borders, zoom/pan, hover spotlight, node-click refocus, a
  readable-zoom clamp, light + dark themes. `Codemap.tsx` renders it instead of
  `<Mermaid>` (Mermaid is still used by the wiki diagrams).
- **Click an edge → exact call site** (the differentiator): anchors flow
  `traverse_graph.get_neighbors` (adds `anchor_file`/`anchor_line` to edge meta,
  additive) → `codeminer/web/codemap.py` (aggregates call sites per `(src,tgt)`
  edge, 1-based) → `CodemapEdge.anchors` (`web/lib/api.ts`) → `CodeGraph` edge tap
  → `CallSitePeek` in `Codemap.tsx` (source window + spotlighted line via
  `HighlightedCode`'s new `highlightLine`). Multi-site edges get a call-site pager.
- **Node click → its definition source** (`SourcePeek` handles both node-def and
  edge-call-site); a "Focus here" button re-roots from a node peek; chips still
  refocus. So *everything on the graph is clickable to real code*: node → def,
  edge → call site.
- **Graph is now the repo's default view** (`page.tsx` default mode `codemap`, tab
  order `Graph | Wiki`); the wiki is one click away (tab, the TOC sidebar, or a
  `?p=<page>` deep-link which forces wiki). The right rail shows "About this graph"
  stating the LSP/SCIP-precision differentiator. This is the **P3 "weaken wiki"**
  step (graph as spine) — short of the deeper "wiki pages *are* graph views".
- **Deeper P3 — wiki pages ARE graph views**: each wiki page leads with a
  "Subsystem map" — `build_page_subgraph` (`codemap.py`) resolves the page's
  citations to graph nodes, adds a 1-hop neighbourhood, and returns an anchored
  induced subgraph (seeds highlighted, self-loops dropped); served at
  `/api/repos/{id}/wiki/{page_id}/graph`. A shared `GraphView` component (Cytoscape
  + source peek) renders it; "Focus here" on a node cross-links into full Graph
  mode (`Codemap` gained an `initialSymbol` prop). Verified: node→def, edge→call
  site, and the wiki→graph jump all work on the wiki page.
- Verified across Go/Python/Rust/TS; backend graph+web unit tests pass (195/10).
- **Honest caveat (matches the thesis):** anchor *precision* is indexer-bound.
  Go (caddy:220) and Python (astropy:1381) landed on the exact call line; one
  Rust (ruff) edge's first occurrence sat ~2 lines off the visible use — SCIP
  occurrence placement, not a numbering bug (the +1 0→1-based conversion is
  correct, since Go/Python are exact). Gaps show as missing/slightly-shifted
  anchors, never fabricated edges.

Still open: **P4** (fine edge types: CALLS/IMPORTS/EXTENDS/IMPLEMENTS from SCIP
roles — colour/label edges by relation). Original design rationale below.

## The problem

Two things to fix at once:

1. **The graph looks crude.** Root cause: the codemap renders through **Mermaid**
   (`web/components/Codemap.tsx` + `codeminer/web/codemap.py`), and Mermaid is a
   *static-diagram* tool. Its auto-layout on a dense call graph is unavoidably
   messy — no zoom/pan, no clustering, no filtering, only click-a-chip-to-refocus.
   No amount of CSS fixes this; it's the wrong rendering layer.
2. **Product looks like a clone.** "Wiki + graph + ask" reads as DeepWiki ∪ GitNexus
   → "就这 / 抄袭". We need a differentiator a layperson can *see* in one interaction.

## Thesis: lead with edge **provenance**

The one capability neither competitor offers — and that we already have the data
for — is **compiler-derived reference edges anchored to exact occurrence sites.**

| | How edges are built | Per-call-site location | Can "click edge → jump to exact line"? |
|---|---|---|---|
| **GitNexus** | tree-sitter AST + heuristic resolution (12-phase DAG, MRO/import inference), every edge carries a **confidence tier** (0.5–0.95) | node-to-node; docs describe no per-invocation file+line | not advertised |
| **DeepWiki** | LLM/RAG; emits prose + diagrams + an Ask interface | line-level **citations** (RAG-selected evidence opening the file on GitHub) | citations, not traversable graph edges |
| **CodeMiner** | **SCIP / clangd** semantic indexes | `anchor_file` + `anchor_line` **on every reference edge** | **yes — once threaded to the UI** |

**Hero interaction:** click an edge → the code pane opens at the *exact call site*,
highlighted. "Verifiable code intelligence, not vibes." That is hard to copy
because GitNexus surfaces node-level relations with confidence tiers but does not
advertise per-call-site occurrence anchors you can traverse, and DeepWiki's
evidence is LLM/RAG-selected, not graph-derived.

### Be honest, don't strawman (these framings are load-bearing)

- **Not "zero hallucinated edges."** SCIP/clangd coverage is bounded — dynamic
  dispatch, reflection, macros/templates, conditional compilation, unindexed deps,
  and partial builds all leave **gaps**. Frame it as: *edges are compiler-derived,
  not heuristically guessed, so they don't fabricate references; coverage is
  bounded by what the indexer resolves — gaps appear as **missing** edges, not
  **wrong** ones.* Scope claims to "anchored to indexer-reported occurrence ranges."
- **DeepWiki is more than prose.** It has clickable line-level citations and warns
  users to verify load-bearing claims. The real difference: its citations are
  RAG/LLM-selected evidence links, **not** a compiler-resolved graph you can
  traverse edge by edge.
- **GitNexus is a strong product.** Real blast-radius/impact, multi-file rename,
  Cypher over a property graph (KuzuDB, now LadybugDB), Leiden clustering, process
  tracing, 16 MCP tools, Sigma.js + Graphology WebGL viz, 14 languages, zero-server
  WASM. Its confidence tiers
  are *honest engineering*, not a flaw. Frame our edge over it as **precision of
  edge provenance / click-to-exact-line**, never "they don't have a graph."

## Direction

### 1. Replace Mermaid with **Cytoscape.js**

Cytoscape is the right fit at demo scale (tens–hundreds of nodes): clean layouts,
rich built-in interactivity, edge-click handlers, easy theming. Target UX:

- **dagre** (directional, for call flow) or **fcose** (clustered) layout.
- **Compound nodes** grouping symbols by file/module.
- **Progressive expand-on-click** — start at one symbol, expand neighbors on demand;
  not a BFS dump.
- **Hover → code preview**; **edge click → open the exact `anchor_line`** (the bet).
- **Filter** by node kind; **search/focus**; zoom/pan/minimap.

*Graduation path:* if we ever render whole-repo-scale graphs, move to **Sigma.js +
Graphology** (WebGL, what GitNexus uses). Not needed for the demo.

### 2. Weaken the wiki — make pages **views over the graph**

The wiki is the most copyable surface. Don't delete it (it's real onboarding
value), but stop shipping it as standalone prose. Reframe: a subsystem page **is** a
saved graph neighborhood + narration, where **every claim carries a clickable
anchor**. Graph is the spine; prose is connective tissue. This sheds "just another
wiki" while keeping the narration layer (`codeminer/wiki/`).

## What to build (the two backend gaps)

**The anchor data already flows end-to-end at the storage/query layer — the drop is
purely in the graph-*consumption* layer.** Providers thread `anchor_file`/`anchor_line`
into `CodeGraph._add_edge` (`codeminer/graph/code_graph.py:329-405`, stored on igraph
edge attrs ~:401-405) from SCIP 5-tuples (`scip_interface/scip_decode_core.py:99-123`
+ per-language decoders) and clangd `.idx` locations (`ls_index/clangd_decode.py:847-889`).
`CodeGraph.query_range` already exposes them via `EdgeRef.anchor_file/anchor_line`
(`code_graph.py:57-69`, `:619-629`). **Do not claim the storage layer lacks anchors.**

**Gap A — thread anchors to the UI (the click-to-source feature).** Each consumer
re-builds edges without the anchor:

1. `traverse_graph.RepoDependencySearcher.get_neighbors` forwards only `{"type": etype}`
   (`codeminer/graph/traverse_graph.py:81-88`) — it has `edge.attributes()` but drops
   the anchors. **This is the upstream origin of the drop**; `DependencyAnalyzer`
   never even receives them.
2. `DependencyAnalyzer._bfs`/`call_path` build `DepEdge(source,target,type)` and
   `DepEdge` has no anchor fields (`codeminer/graph/dependency.py:59-66`, `:154-160`,
   `:193-201`).
3. MCP `dependency_subgraph` serializes that anchor-less edge
   (`codeminer/mcp/tools/dependency.py:48`, `codeminer/mcp/server.py:178-201`).
4. Web codemap edges are `{source,target}` node-ids only — no type, no anchor, and
   the node `line` is the symbol's *own definition* line, not the call site
   (`codeminer/web/codemap.py:203-207`; `web/lib/api.ts` `CodemapEdge`:143-146).

   *Plan:* add `anchor_file`/`anchor_line` to `DepEdge` + `to_dict`; have
   `get_neighbors` carry the edge's anchor attrs into the tuple; thread through
   `DependencyResult.to_dict` → MCP JSON → `codemap.py` edge dicts → `CodemapEdge`;
   handle edge-click in the new Cytoscape component to open the code pane at the anchor.
   *(Alternative: read `CodeGraph.query_range`/`EdgeRef` directly, which already carry anchors.)*

   ⚠️ **Per-call-site dedup nuance:** `codemap.py` dedups edges by `(src,tgt)` (its
   `edge_seen` set, `codemap.py:153-170`), collapsing multiple call sites between the
   same pair; `get_neighbors` doesn't dedup but already drops the anchors upstream. To
   keep **one clickable edge per call site**, carry anchors through `get_neighbors`
   **and** drop `codemap.py`'s `(src,tgt)` dedup.

**Gap B — coarse edge types (stretch, scope later).** Only `EDGE_TYPE_CONTAIN="contain"`
and `EDGE_TYPE_REFERENCE="reference"` exist (`codeminer/types.py:16-17`). There is no
CALLS/IMPORTS/EXTENDS/IMPLEMENTS distinction — even clangd inheritance/override fold
into reference-class edges (`ls_index/clangd_decode.py:891+`). GitNexus distinguishes
these. SCIP symbol roles can recover some; bigger backend change — defer.

## Reuse, don't reinvent (graph ops that already exist)

- `DependencyAnalyzer` (`codeminer/graph/dependency.py`): `impact()` (blast radius,
  `:108-115`), `dependencies()` (callees, `:117-121`), `subgraph()` (1-hop, `:123-125`),
  `call_path()` (shortest A→B, `:127-161`).
- MCP `dependency_subgraph` (`codeminer/mcp/tools/dependency.py:20-48`) →
  `{root,direction,nodes,edges,truncated,note}`.
- Agent skills `find_callees`/`find_callers`/`trace` (`codeminer/agent/skills/_graphnav.py`).
  ⚠️ `find_callees` executor docstring says "incoming" but returns callees (stale
  docstring; behavior is correct). ⚠️ `impact_analysis` skill dir has no source
  `executor.py` in the checkout (only `__pycache__`) — verify before relying on it.

## Rough roadmap (next round, not this one)

- **P1** — Gap A: thread anchors (`DepEdge` → MCP → web), add a Cytoscape component behind a flag.
- **P2** — Cytoscape UX: compound nodes, expand-on-click, filter, **edge click → exact source**.
- **P3** — Wiki-as-graph-views reframe.
- **P4 (stretch)** — Gap B: fine-grained edge types from SCIP roles.

## Verify the bet end-to-end

After P1–P2: open the codemap on a C/C++ repo (clangd-indexed, e.g. `redis/redis`)
and a Python repo (SCIP-indexed), click an edge, and confirm the code pane lands on
the **exact call-site line** (cross-check against `CodeGraph.query_range` anchors).
Confirm distinct call sites between the same two symbols render as **separate**
clickable edges (dedup nuance above).
