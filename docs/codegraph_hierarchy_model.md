# CodeGraph Hierarchy Model

The CodeGraph UI should not render the raw LSP/SCIP reference graph as the
primary structure. Reference graphs are dense, cyclic, and quickly become a
hairball. The model we want is a compound graph:

```text
G = (V, containment_edges, dependency_edges)
```

- `containment_edges` form a tree: directory -> file -> symbol.
- `dependency_edges` are the call/reference/import overlay.

This lets the UI use the containment tree as the visual backbone and draw only a
small number of important cross-edges on top.

## Current API shape

`codenib.graph.hierarchy` builds a reusable repo-level
`HierarchicalCodeGraph` from the indexed `CodeGraph`. The web codemap response
projects that repo-level structure onto the current focus window, so existing
clients still receive compact `nodes`/`edges` plus the relevant containment
ancestors:

```ts
interface CodemapResponse {
  nodes: CodemapNode[];       // symbol nodes in the current focus window
  edges: CodemapEdge[];       // reference/call overlay
  hierarchy: CodemapHierarchy;
}

interface CodemapEdge {
  source: string;              // CodemapNode id
  target: string;
  anchors?: CallSite[];
  weight?: number;
  source_hierarchy?: string;   // hier::symbol::<source>
  target_hierarchy?: string;   // hier::symbol::<target>
  bundle_path?: string[];      // source -> LCA -> target through containment
  bundle_lca?: string;
  cross_file?: boolean;
}

interface CodemapHierarchyNode {
  id: string;
  parent: string | null;
  kind: "root" | "directory" | "file" | "symbol";
  label: string;
  path?: string;
  file?: string;
  node_id?: string;           // present for symbol hierarchy leaves
  line?: number;
  end_line?: number;
  depth: number;
  child_count: number;
  symbol_count: number;
  doi: number;                // importance - distance from focus
  open_by_default?: boolean;
}
```

`hierarchy.open_files` is a backend Degree-of-Interest expansion hint. The UI
can cap it further for viewport constraints, but the ranking belongs to the data
model rather than to ad hoc frontend path parsing. `edges[].bundle_path` connects
the call/reference overlay back to the containment tree, which is the data needed
for true hierarchical edge bundling.

## Current frontend pass

`web/components/CodeGraph.tsx` now prefers the backend hierarchy and falls back
to deriving it from `node.file` only for older payloads. It renders:

- directory compound boxes from `hierarchy.nodes`,
- file pills/boxes under those directories,
- symbol scope boxes (class/container symbols) under expanded files,
- concrete symbol nodes inside those scopes,
- reference edges between the currently visible endpoints,
- scope-aware styling for exact same-file references: those Cytoscape edges
  carry a `scopeRoute` attribute so class/member calls read differently from
  cross-file edges, and the edge remains clickable for exact source peeks.

Hierarchical SVG edge bundling for cross-file aggregate edges is **not yet
consumed by the frontend** — see the planned subsection below; the backend
already emits `edges[].bundle_path` for it.

The shipped UI is a single files-overview mode: `GraphMode` is hardcoded to
`"files"` in `CodeGraph.tsx`, and there is no `Files`/`Symbols` switch. The
graph opens with one pill per file grouped under directory compounds. Clicking
a file toggles its symbols and scope tree inline while preserving the
directory/file projection; clicking a concrete symbol opens its source, and
clicking an edge opens the exact SCIP/LSP call site. The global `Fit` action
remains available to restore the whole graph.

Both the wiki embedded map and the standalone explorer use this same
files-overview component, and its reader-facing controls stay minimal: an edge
legend, a compact status note (file count or current selection), and `Fit`.

The standalone graph explorer defaults to a one-hop focus window. This follows
Sourcetrail's focus+context pattern: start with the current symbol and immediate
callers/callees, then let the user explicitly expand to two hops when they need
broader context.

The Cytoscape layer also applies semantic zoom classes:

- low zoom: concrete symbol nodes become small landmarks and hide labels, while
  directory/file/scope labels remain visible;
- mid zoom: concrete symbol labels return in a compact style;
- high zoom / hover / focus: exact symbol labels are fully readable and still
  open source peeks without rebuilding the graph instance.

### Planned / not yet consumed by the frontend

Earlier design passes sketched richer presentation that the backend already
supports but the shipped component does not render:

- hierarchical SVG edge bundling for cross-file aggregate edges: the backend's
  `attach_edge_routes` (`codenib/graph/hierarchy.py`) emits
  `edges[].bundle_path`/`bundle_lca`, and the fields are typed in
  `web/lib/api.ts`, but no component consumes them yet;
- `Map` / `Routes` / `Raw` edge-layer controls (compound-first, route-emphasis,
  and raw-edge debugging views) — these presentation layers have no UI today;
- a `Files`/`Symbols` mode switch with a DOI-driven `Symbols` spine — the
  shipped component hardcodes the files overview and leaves expansion to the
  user.

## Current backend model

The backend model now separates containment and dependency edges before any
view-specific filtering. The separation is backed by the reusable graph-layer
API in `codenib.graph.layers`, so callers can query the same indexed
`CodeGraph` as overlapping relation graphs (`all`, `containment`, `dependency`,
`reference`, `import`, and `type-use`) without changing the persisted graph
schema:

```ts
interface ContainmentNode {
  id: string;
  parent: string | null;
  kind: "root" | "directory" | "file" | "symbol";
  label: string;
  file?: string;
  node_id?: string;            // canonical CodeGraph symbol identity
  line?: number;               // 1-based display line
  end_line?: number;
  importance?: number;
}

interface DependencyEdge {
  source: string;
  target: string;
  kind: "reference" | "import" | "type-use";
  weight: number;
  anchors: CallSite[];
}

interface HierarchicalCodeGraph {
  root: string;
  containment: ContainmentNode[];
  dependencies: DependencyEdge[];
  source_root: string;
}
```

The repo bundle builds this object lazily and caches it in memory. The codemap
projection remaps visible symbol ids to frontend ids (`n0`, `n1`, ...), keeps
invisible ancestor symbols in the route when the index has `contain` edges, and
uses a dominant source-root heuristic so root files such as `setup.py` do not
make package labels noisy.

When explicit `contain` edges are missing, the backend now has a conservative
fallback: within a file, container-like symbols such as classes can parent
members whose readable names share the same scope prefix (`Widget` ->
`Widget.run()`) and whose source ranges fit inside the container. This keeps the
compound graph from collapsing to a flat file whenever an index lacks explicit
scope edges but still exposes SCIP/LSP-style symbol names.

## DOI and semantic zoom

The current implementation uses DOI-style ranking in two places:

- the backend computes `doi`/`open_by_default` hints for the current focus
  window, combining reference distance and containment-tree distance from the
  focus symbol;
- the frontend keeps every file folded as a pill and expands it manually on
  click. Hierarchy scope nodes fall back to `doi` for their `importance` value,
  which drives node font size, padding, and layout ordering; the
  `open_by_default`/`open_files` hints ride along in the payload as expansion
  hints the UI does not yet auto-apply.

The ranking follows the fisheye shape:

```text
doi(node) = importance(node) - distance(node, focus)
```

Nodes above threshold are expanded; the rest stay folded at directory/file level.
The UI also applies semantic zoom:

- low zoom keeps concrete symbols as landmarks and hides most labels;
- hover/focus temporarily reveals exact labels without rebuilding Cytoscape;
- mid/high zoom reveals symbol labels and supports source peeks.

## Remaining model upgrades

- Promote the current in-memory expansion overrides into a route-level graph
  session state if we need them to survive modal close/reopen or page navigation.
- Add decoders for import/type-use/read-write edges; the layer API already has
  empty buckets for import and type-use so new decoders do not need a separate
  graph-view contract.

## References

- Compound digraphs: Kozo Sugiyama and Kazuo Misue, "Visualization of structural
  information: automatic drawing of compound digraphs."
- Degree-of-interest / fisheye views: George W. Furnas, "Generalized Fisheye
  Views."
- Hierarchical edge bundling: Danny Holten, "Hierarchical Edge Bundles:
  Visualization of Adjacency Relations in Hierarchical Data."
- Interaction reference: Sourcetrail, especially its focus-and-context graph
  navigation model.
