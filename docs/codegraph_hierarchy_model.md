<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeGraph Hierarchy Model

CodeNib presents a repository graph as two related structures:

- a containment tree: repository → directory → file → symbol;
- a reference overlay: symbol-to-symbol relationships with source anchors.

The containment tree keeps a repository readable when its reference graph is
dense or cyclic. The reference overlay preserves the dependency information
needed for callers, callees, and exact source navigation.

## Codemap API

`GET /api/repos/{repo_id}/codemap` returns a focused dependency view. The
request accepts:

| Parameter | Default | Description |
| --- | --- | --- |
| `symbol` | busiest indexed symbol | Symbol to focus; fuzzy resolution is handled by the backend. |
| `direction` | `both` | `both`, `callers`, or `callees`. |
| `depth` | `2` | Reference traversal depth. The web explorer sends `1` by default and lets the reader select `2`. |
| `max_nodes` | `40` | Upper bound for symbols in the response. |
| `commit` | newest available snapshot | Commit from a prebuilt commit window, when present. |

The response contains compact `nodes` and `edges` for the selected window plus
the matching containment ancestors:

```ts
interface CodemapResponse {
  available: boolean;
  root?: string;
  root_label?: string;
  direction?: string;
  depth?: number;
  truncated?: boolean;
  nodes: CodemapNode[];
  edges: CodemapEdge[];
  hierarchy?: CodemapHierarchy;
  mermaid: string;
  note?: string;
  setup?: GraphSetupReport;
  commit?: string | null;
  fell_back?: boolean;
}

interface CodemapEdge {
  source: string;
  target: string;
  anchors?: Array<{ file: string; line: number | null }>;
  weight?: number;
  source_hierarchy?: string;
  target_hierarchy?: string;
  bundle_path?: string[];
  bundle_lca?: string;
  bundle_lca_kind?: string;
  cross_file?: boolean;
}

interface CodemapHierarchy {
  root: string;
  nodes: CodemapHierarchyNode[];
  open_files?: string[];
  source_root?: string;
}

interface CodemapHierarchyNode {
  id: string;
  parent: string | null;
  kind: "root" | "directory" | "file" | "symbol";
  label: string;
  path?: string;
  file?: string;
  node_id?: string;
  line?: number;
  end_line?: number;
  depth: number;
  child_count: number;
  symbol_count: number;
  doi: number;
  importance?: number;
  open_by_default?: boolean;
  external?: boolean;
}
```

`node_id` connects a hierarchy symbol to the compact id used by
`CodemapResponse.nodes`. Edge anchors and hierarchy line numbers are 1-based so
they can be shown directly in the source viewer.

If the selected commit snapshot cannot be loaded, the endpoint serves the
repository's default graph and sets `fell_back`. If no dependency graph is
available, it returns `available: false` together with repository-specific
setup information.

## Backend construction

`codenib.graph.hierarchy.build_hierarchical_code_graph` builds one
`HierarchicalCodeGraph` from the loaded `CodeGraph`. A repository bundle builds
this object lazily and caches it in memory.

The builder:

1. creates directory and file nodes from source paths;
2. attaches symbols using explicit `contain` edges;
3. conservatively infers a same-file container when explicit symbol
   containment is absent and names and source ranges establish the parent;
4. aggregates `reference` edges by source and target while preserving distinct
   call-site anchors;
5. records symbol importance, depth, descendant counts, and external symbols.

For each codemap request, `hierarchy_for_view` projects the repository-level
tree onto the focused symbols and retains the ancestors required to display
them. It computes Degree-of-Interest values from graph importance and distance
from the focus, then emits `open_by_default` and `open_files` hints.

`attach_edge_routes` maps every visible reference edge back to its hierarchy
leaves. It adds the source and target hierarchy ids, the route through their
lowest common ancestor, and whether the edge crosses files.

## Web presentation

The Dependency Map has two current views:

- **Structure** is the default. It renders the projected hierarchy as an
  expandable directory/file/symbol tree. Relationships are rolled up to the
  nearest visible ancestors, and expanding a container reveals finer-grained
  endpoints. Edge anchors remain available when relationships are aggregated.
- **Graph** renders a Cytoscape file overview. Files start as collapsed pills
  grouped by directory. Clicking a file expands its symbol and scope
  containers; clicking a concrete symbol or an exact edge opens the
  corresponding source.

The explorer also exposes focus-symbol, direction, and one-hop/two-hop
controls. Its graph view applies semantic zoom: concrete symbol labels are
reduced at low zoom and restored at closer zoom, while file and scope labels
remain visible. `Fit` restores the full graph to the viewport.

Wiki pages use the same hierarchy payload. When it is present, their embedded
map renders the expandable Structure view; older payloads fall back to a flat
system map.
