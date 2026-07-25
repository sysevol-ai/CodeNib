# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build a dependency "codemap" subgraph for the web demo.

Walks the per-repo symbol graph's REFERENCE (call/use) edges out from a seed
symbol and returns a compact ``{nodes, edges}`` payload plus a Mermaid
``graph LR`` rendering the frontend can drop into the existing ``<Mermaid>``
component. Labels use the graph's ``unified_name`` (``file:Symbol()``) so the
map reads as code, not content-hash identities.

This is deliberately self-contained (no dependency on the ``DependencyAnalyzer``
plugin, which postdates this branch) — it walks the graph through the
``RepoDependencySearcher`` traversal helper that ships on the branch.
"""

from __future__ import annotations

import os
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

from ..graph.code_graph import CodeGraph
from ..graph.hierarchy import (
    HierarchicalCodeGraph,
    attach_edge_routes,
    empty_hierarchy,
    hierarchy_for_view,
    hierarchy_from_view_nodes,
)
from ..graph.traverse_graph import RepoDependencySearcher

# Node types eligible as a focus / default seed (skip file/dir/import nodes).
_SYMBOL_TYPES = frozenset({"function", "method", "class"})
# Recognized source-file extensions (fallback when the repo root isn't known).
_SOURCE_EXTS = frozenset(
    {
        ".py",
        ".pyx",
        ".pyi",
        ".go",
        ".rs",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cxx",
        ".java",
        ".rb",
        ".php",
        ".cs",
        ".kt",
        ".kts",
        ".swift",
        ".scala",
        ".m",
        ".mm",
    }
)
_MERMAID_MAX_LABEL = 42
_DIR_TO_MODE = {"both": "all", "callees": "forward", "callers": "backward"}


def _attrs(graph: CodeGraph, name: str) -> Dict[str, Any]:
    try:
        return graph.get_node_info_by_name(name) or {}
    except Exception:  # noqa: BLE001 - a missing/odd node must not 500 the map
        return {}


def _label(attrs: Dict[str, Any], identity: str) -> str:
    return (attrs.get("unified_name") or "").strip() or identity


def _short(label: str) -> str:
    """``pkg/dir/file.py:Class.method()`` -> ``file.py:Class.method()`` (capped)."""
    if ":" in label:
        path, _, sym = label.partition(":")
        base = path.rsplit("/", 1)[-1]
        short = f"{base}:{sym}" if sym else base
    else:
        short = label.rsplit("/", 1)[-1]
    if len(short) > _MERMAID_MAX_LABEL:
        short = short[: _MERMAID_MAX_LABEL - 1] + "…"
    return short


def _resolve(graph: CodeGraph, symbol: str) -> Optional[str]:
    """Map a user-entered symbol to a graph identity name.

    Tries an exact identity match first, then a case-insensitive match on the
    readable ``unified_name`` (exact, then shortest substring match, preferring
    function/method/class nodes).
    """
    if symbol in graph.name_to_vertex:
        return symbol
    needle = symbol.strip().lower()
    if not needle:
        return None
    best: Optional[str] = None
    best_score = float("inf")
    for v in graph.get_graph().vs:
        a = v.attributes()
        uni = a.get("unified_name") or ""
        if not uni:
            continue
        low = uni.lower()
        if low == needle:
            return a["name"]
        if needle in low:
            score = len(uni) - (1000 if a.get("type") in _SYMBOL_TYPES else 0)
            if score < best_score:
                best_score, best = score, a["name"]
    return best


def _default_seed(graph: CodeGraph) -> Optional[str]:
    """Highest reference out-degree node that is a named function/method/class."""
    g = graph.get_graph()
    out_ref: Dict[int, int] = {}
    for e in g.es:
        if e.attributes().get("type") == "reference":
            out_ref[e.source] = out_ref.get(e.source, 0) + 1
    best: Optional[str] = None
    best_count = -1
    for vid, count in out_ref.items():
        a = g.vs[vid].attributes()
        if (
            a.get("unified_name")
            and a.get("type") in _SYMBOL_TYPES
            and count > best_count
        ):
            best_count, best = count, a["name"]
    if best is None:  # graph with no usable reference edges — any named node
        for v in g.vs:
            if v.attributes().get("unified_name"):
                return v["name"]
    return best


def _is_external(a: Dict[str, Any], repo_dir: Optional[str] = None) -> bool:
    """True if a node has no openable in-repo source.

    External library symbols carry a dotted module path (e.g.
    ``setuptools.extension``) and no line; file/dir nodes carry no line either.
    When the repo root is known, "openable" means the file actually exists under
    it — mirroring the ``/source`` endpoint — so repo-root files like
    ``setup.py`` / ``main.go`` are NOT misflagged. Without a root, fall back to:
    needs a line, and the file is a path or has a known source extension.
    """
    f = a.get("file")
    s = a.get("start_line")
    if not isinstance(s, int) or not isinstance(f, str) or not f:
        return True
    if repo_dir:
        safe = os.path.normpath(f).lstrip(os.sep).lstrip("/")
        return not os.path.isfile(os.path.join(repo_dir, safe))
    return ("/" not in f) and (os.path.splitext(f)[1].lower() not in _SOURCE_EXTS)


def _score(
    graph: CodeGraph, names: List[str]
) -> Tuple[Dict[str, float], Dict[str, int], Dict[str, int], Dict[str, float]]:
    """PageRank importance + community + reference count + entry score per node.

    All computed on the induced REFERENCE subgraph of *names* (cheap, <=120
    nodes). PageRank (rank-percentile in [0,1]) surfaces "core" symbols
    (referenced by other important code); community is Leiden/modularity on the
    undirected projection (igraph 0.11.9 has no directed Leiden), guarded so
    tiny/loose graphs collapse to one cluster; ``ref_count`` = in-degree (how
    many symbols reference this one); ``entry_score`` = out/(in+out) in [0,1] —
    high for drivers/entry points that call much but are called little. Never
    raises — returns neutral scores (0.0 / 0) on any failure.
    """
    imp: Dict[str, float] = {n: 0.0 for n in names}
    comm: Dict[str, int] = {n: 0 for n in names}
    refcnt: Dict[str, int] = {n: 0 for n in names}
    entry: Dict[str, float] = {n: 0.0 for n in names}
    try:
        g = graph.get_graph()
        n2v = graph.name_to_vertex
        vids = [n2v[n] for n in names if n in n2v]
        if len(vids) < 3:
            return imp, comm, refcnt, entry
        sub = g.subgraph(vids)
        ref_eids = [
            e.index for e in sub.es if e.attributes().get("type") == "reference"
        ]
        subref = sub.subgraph_edges(ref_eids, delete_vertices=False)
        sub_names = list(subref.vs["name"])
        pr = subref.pagerank(directed=True, damping=0.85)
        order = sorted(range(len(pr)), key=lambda i: pr[i])
        denom = max(1, len(pr) - 1)
        for rank, i in enumerate(order):
            imp[sub_names[i]] = round(rank / denom, 4)
        indeg = subref.indegree()
        outdeg = subref.outdegree()
        for i, nm in enumerate(sub_names):
            refcnt[nm] = int(indeg[i])
            tot = indeg[i] + outdeg[i]
            entry[nm] = round(outdeg[i] / tot, 4) if tot else 0.0
        membership = [0] * subref.vcount()
        if subref.vcount() >= 12:
            try:
                gu = subref.as_undirected(mode="collapse")
                cl = gu.community_leiden(
                    objective_function="modularity", n_iterations=-1
                )
                if cl.modularity is not None and cl.modularity >= 0.2:
                    membership = list(cl.membership)
            except Exception:  # noqa: BLE001 - community is optional polish
                pass
        for i, nm in enumerate(sub_names):
            comm[nm] = int(membership[i])
    except Exception:  # noqa: BLE001 - scoring is best-effort, never 500 the map
        pass
    return imp, comm, refcnt, entry


def _enrich(
    graph: CodeGraph,
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    hub_cap: int = 8,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Tag nodes with importance/community and prune high-out-degree hubs.

    A node with more than *hub_cap* outgoing edges keeps only the edges to its
    highest-importance targets; the rest are folded away and counted in
    ``hidden_callees`` so the UI can badge "+N". Nodes left fully disconnected by
    that fold (the low-value leaf tail) are dropped unless they're a seed/root.
    This keeps dense, high-fan-out maps from becoming hairballs.
    """
    imp, comm, refcnt, entry = _score(graph, [n["name"] for n in nodes])
    for n in nodes:
        n["importance"] = imp.get(n["name"], 0.0)
        n["community"] = comm.get(n["name"], 0)
        n["ref_count"] = refcnt.get(n["name"], 0)
        n["entry_score"] = entry.get(n["name"], 0.0)
    # Edge weight = number of distinct SCIP/LSP-exact call sites (anchors); drives
    # edge width in the UI. Set on all edges (both codemap paths call _enrich), so
    # the kept subset below carries it too.
    for e in edges:
        e["weight"] = len(e.get("anchors") or [])

    imp_by_id = {n["id"]: n.get("importance", 0.0) for n in nodes}
    by_src: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges:
        by_src.setdefault(e["source"], []).append(e)
    kept: List[Dict[str, Any]] = []
    hidden: Dict[str, int] = {}
    for src, es in by_src.items():
        if len(es) <= hub_cap:
            kept.extend(es)
            continue
        es_sorted = sorted(
            es, key=lambda e: imp_by_id.get(e["target"], 0.0), reverse=True
        )
        kept.extend(es_sorted[:hub_cap])
        hidden[src] = len(es) - hub_cap
    for n in nodes:
        if n["id"] in hidden:
            n["hidden_callees"] = hidden[n["id"]]
    connected = {e["source"] for e in kept} | {e["target"] for e in kept}
    nodes = [n for n in nodes if n["id"] in connected or n.get("is_root")]
    return nodes, kept


def build_codemap(
    graph: CodeGraph,
    symbol: Optional[str] = None,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 40,
    repo_dir: Optional[str] = None,
    hierarchy_graph: Optional[HierarchicalCodeGraph] = None,
) -> Dict[str, Any]:
    """Return a dependency subgraph + Mermaid rendering around *symbol*.

    Args:
        graph: the repo's loaded :class:`CodeGraph`.
        symbol: focus symbol (readable name); ``None`` picks a central default.
        direction: ``"both"`` (callers + callees), ``"callees"`` (what it calls),
            or ``"callers"`` (what calls it).
        depth: BFS hops over reference edges (clamped to 1..4).
        max_nodes: node cap (clamped to 2..120); BFS stops adding past it.
    """
    direction = direction if direction in _DIR_TO_MODE else "both"
    depth = max(1, min(int(depth), 4))
    max_nodes = max(2, min(int(max_nodes), 120))
    note = ""

    root = _resolve(graph, symbol) if symbol else None
    if symbol and root is None:
        note = f"Symbol {symbol!r} not found — showing the repo's busiest symbol."
    if root is None:
        root = _default_seed(graph)
    if root is None:
        return {
            "available": False,
            "root": "",
            "root_label": "",
            "direction": direction,
            "nodes": [],
            "edges": [],
            "hierarchy": empty_hierarchy(),
            "mermaid": "",
            "note": "This repo has no symbol graph.",
        }

    searcher = RepoDependencySearcher(graph)
    mode = _DIR_TO_MODE[direction]

    seen: Set[str] = {root}
    depth_of: Dict[str, int] = {root: 0}
    raw_edges: List[Tuple[str, str]] = []
    edge_seen: Set[Tuple[str, str]] = set()
    # Aggregate the exact reference (call) site(s) per edge so the UI can jump
    # to source. One displayed edge collects several call sites (a list) rather
    # than multiplying edges, which keeps the graph readable.
    edge_anchors: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    queue: deque[str] = deque([root])
    truncated = False

    while queue:
        cur = queue.popleft()
        d = depth_of[cur]
        if d >= depth:
            continue
        neighbors, n_edges = searcher.get_neighbors(
            cur, direction=mode, etype_filter={"reference"}, ignore_test_file=True
        )
        for src, tgt, _w, meta in n_edges:
            if src == tgt:
                continue  # drop self-loops (recursion) — visual noise
            key = (src, tgt)
            if key not in edge_seen:
                edge_seen.add(key)
                raw_edges.append(key)
            af = meta.get("anchor_file") if isinstance(meta, dict) else None
            al = meta.get("anchor_line") if isinstance(meta, dict) else None
            if af:
                anchors = edge_anchors.setdefault(key, [])
                # anchor_line is 0-based (tree-sitter/LSP); expose 1-based.
                entry = {"file": af, "line": (al + 1) if isinstance(al, int) else None}
                if entry not in anchors:
                    anchors.append(entry)
        for nb in neighbors:
            if nb in seen:
                continue
            if len(seen) >= max_nodes:
                truncated = True
                continue
            seen.add(nb)
            depth_of[nb] = d + 1
            queue.append(nb)

    id_of: Dict[str, str] = {}
    nodes: List[Dict[str, Any]] = []
    for i, name in enumerate(sorted(seen, key=lambda n: (depth_of.get(n, 9), n))):
        a = _attrs(graph, name)
        label = _label(a, name)
        start = a.get("start_line")
        nid = f"n{i}"
        id_of[name] = nid
        nodes.append(
            {
                "id": nid,
                "name": name,
                "label": label,
                "short": _short(label),
                "file": a.get("file"),
                "line": (start + 1) if isinstance(start, int) else None,
                "end_line": (
                    (a.get("end_line") + 1)
                    if isinstance(a.get("end_line"), int)
                    else None
                ),
                "kind": a.get("type") or "symbol",
                "depth": depth_of.get(name, 0),
                "is_root": name == root,
                "external": _is_external(a, repo_dir),
            }
        )

    edges: List[Dict[str, Any]] = []
    for s, t in raw_edges:
        if s not in id_of or t not in id_of:
            continue
        edge: Dict[str, Any] = {"source": id_of[s], "target": id_of[t]}
        anchors = edge_anchors.get((s, t))
        if anchors:
            edge["anchors"] = anchors
        edges.append(edge)

    nodes, edges = _enrich(graph, nodes, edges)
    hierarchy = (
        hierarchy_for_view(hierarchy_graph, nodes)
        if hierarchy_graph is not None
        else hierarchy_from_view_nodes(nodes)
    )
    edges = attach_edge_routes(edges, hierarchy)
    return {
        "available": True,
        "root": root,
        "root_label": _label(_attrs(graph, root), root),
        "direction": direction,
        "depth": depth,
        "truncated": truncated,
        "nodes": nodes,
        "edges": edges,
        "hierarchy": hierarchy,
        "mermaid": _to_mermaid(nodes, edges, id_of[root]),
        "note": note,
    }


def _mm_escape(text: str) -> str:
    """Neutralize characters that break a Mermaid quoted node label."""
    return text.replace('"', "'").replace("\n", " ").replace("[", "(").replace("]", ")")


def _to_mermaid(
    nodes: List[Dict[str, Any]], edges: List[Dict[str, str]], root_id: str
) -> str:
    lines = ["graph LR"]
    for n in nodes:
        # The ["..."] are Mermaid node-label syntax, not Python repr quoting.
        lines.append(f'  {n["id"]}["{_mm_escape(n["short"])}"]')  # noqa: B907
    for e in edges:
        lines.append(f'  {e["source"]} --> {e["target"]}')
    # Force readable labels: white box + dark text (the Mermaid neutral theme
    # otherwise renders low-contrast grey-on-grey on a dense graph).
    lines.append("  classDef default fill:#ffffff,stroke:#c9ccd1,color:#1f2937;")
    lines.append("  classDef root fill:#2d7ff9,stroke:#1b5fd0,color:#ffffff;")
    lines.append(f"  class {root_id} root;")
    return "\n".join(lines)


def _resolve_citation(
    graph: CodeGraph,
    cit: Dict[str, Any],
    by_loc: Dict[Tuple[str, int], str],
    by_file: Dict[str, List[Tuple[int, str]]],
) -> Optional[str]:
    """Map a wiki citation (file + 1-based line, or node_name) to a graph identity.

    Prefers an exact ``(file, 0-based start)`` hit, then the nearest symbol start
    in that file (small tolerance), then a readable-name match.
    """
    f = cit.get("file")
    sl = cit.get("start_line")
    if f and isinstance(sl, int):
        s0 = sl - 1  # citations are 1-based (CodeLocation); the graph is 0-based
        if (f, s0) in by_loc:
            return by_loc[(f, s0)]
        best, best_d = None, 3
        for s, name in by_file.get(f, []):
            d = abs(s - s0)
            if d < best_d:
                best, best_d = name, d
        if best:
            return best
    nm = cit.get("node_name")
    return _resolve(graph, nm) if nm else None


def build_page_subgraph(
    graph: CodeGraph,
    citations: List[Dict[str, Any]],
    max_nodes: int = 18,
    repo_dir: Optional[str] = None,
    hierarchy_graph: Optional[HierarchicalCodeGraph] = None,
) -> Dict[str, Any]:
    """Page-evidence-first reference subgraph over a wiki page's cited symbols.

    Resolves each citation to a graph node, then returns the cited nodes plus
    direct reference edges between them. It admits only a tiny number of
    non-cited bridge symbols, and only when a bridge touches multiple cited
    symbols. That keeps the wiki map grounded in the page's evidence instead of
    dragging in a one-hop neighbourhood that may be true code but not relevant
    to the page.
    """
    max_nodes = max(2, min(int(max_nodes), 40))
    g = graph.get_graph()

    # Location + name index for resolving citations precisely (built once).
    by_loc: Dict[Tuple[str, int], str] = {}
    by_file: Dict[str, List[Tuple[int, str]]] = {}
    for v in g.vs:
        a = v.attributes()
        f = a.get("file")
        s = a.get("start_line")
        if not f or not isinstance(s, int) or not a.get("unified_name"):
            continue
        by_loc.setdefault((f, s), a["name"])
        by_file.setdefault(f, []).append((s, a["name"]))

    names: List[str] = []
    chosen: Set[str] = set()
    for cit in citations:
        nm = _resolve_citation(graph, cit, by_loc, by_file)
        if nm and nm not in chosen:
            chosen.add(nm)
            names.append(nm)
        if len(names) >= max_nodes:
            break

    if not names:
        return {
            "available": False,
            "root": "",
            "root_label": "",
            "direction": "both",
            "nodes": [],
            "edges": [],
            "hierarchy": empty_hierarchy(),
            "mermaid": "",
            "note": "No graph symbols for this page.",
        }

    seeds = set(names)  # the page's own cited symbols (highlighted)
    searcher = RepoDependencySearcher(graph)

    direct_edges: List[Tuple[str, str]] = []
    direct_seen: Set[Tuple[str, str]] = set()
    bridge_edges: Dict[str, Set[Tuple[str, str]]] = {}
    bridge_touches: Dict[str, Set[str]] = {}
    bridge_weight: Dict[str, int] = {}
    candidate_order: Dict[str, int] = {}
    edge_anchors: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    def add_anchor(key: Tuple[str, str], meta: Any) -> None:
        af = meta.get("anchor_file") if isinstance(meta, dict) else None
        al = meta.get("anchor_line") if isinstance(meta, dict) else None
        if not af:
            return
        anchors = edge_anchors.setdefault(key, [])
        entry = {"file": af, "line": (al + 1) if isinstance(al, int) else None}
        if entry not in anchors:
            anchors.append(entry)

    for seed in names:
        _, n_edges = searcher.get_neighbors(
            seed, direction="all", etype_filter={"reference"}, ignore_test_file=True
        )
        for src, tgt, _w, meta in n_edges:
            if src == tgt:
                continue
            key = (src, tgt)
            add_anchor(key, meta)
            if src in seeds and tgt in seeds:
                if key not in direct_seen:
                    direct_seen.add(key)
                    direct_edges.append(key)
                continue

            other = tgt if src == seed else src if tgt == seed else None
            if other is None or other in seeds:
                continue
            other_attrs = _attrs(graph, other)
            if _is_external(other_attrs, repo_dir):
                continue
            candidate_order.setdefault(other, len(candidate_order))
            bridge_edges.setdefault(other, set()).add(key)
            bridge_touches.setdefault(other, set()).add(seed)
            bridge_weight[other] = bridge_weight.get(other, 0) + max(
                1, len(edge_anchors.get(key) or [])
            )

    # A bridge has to explain relationships among page evidence, not merely be a
    # random one-hop neighbour of one citation. Rank by exact call-site evidence
    # first so broad generic types like Context do not crowd out stronger domain
    # connectors.
    bridge_candidates = [
        name for name, touches in bridge_touches.items() if len(touches) >= 2
    ]
    bridge_candidates.sort(
        key=lambda name: (
            -bridge_weight.get(name, 0),
            -len(bridge_touches.get(name, set())),
            candidate_order.get(name, 0),
            name,
        )
    )
    bridge_budget = min(2, max(0, max_nodes - len(names)))
    bridges = bridge_candidates[:bridge_budget]
    names = names[:max_nodes] + [b for b in bridges if b not in chosen]
    names = names[:max_nodes]
    nameset = set(names)

    edge_order: List[Tuple[str, str]] = []
    edge_seen: Set[Tuple[str, str]] = set()
    for key in direct_edges:
        if key[0] in nameset and key[1] in nameset and key not in edge_seen:
            edge_seen.add(key)
            edge_order.append(key)
    for bridge in bridges:
        for key in sorted(bridge_edges.get(bridge, set())):
            if key[0] in nameset and key[1] in nameset and key not in edge_seen:
                edge_seen.add(key)
                edge_order.append(key)

    # Collect anchors for any direct seed-seed edges that were not observed from
    # the first seed's neighbour walk due traversal direction/order.
    for nm in nameset:
        _, n_edges = searcher.get_neighbors(
            nm, direction="all", etype_filter={"reference"}, ignore_test_file=True
        )
        for src, tgt, _w, meta in n_edges:
            if src == tgt or src not in nameset or tgt not in nameset:
                continue
            key = (src, tgt)
            touches_seed = src in seeds or tgt in seeds
            if not touches_seed:
                continue
            if key not in edge_seen and src in seeds and tgt in seeds:
                edge_seen.add(key)
                edge_order.append(key)
            if key in edge_seen:
                add_anchor(key, meta)

    # Order seeds first (they're the page's subject); neighbours follow.
    ordered = [n for n in names if n in seeds] + [n for n in names if n not in seeds]
    id_of: Dict[str, str] = {}
    nodes: List[Dict[str, Any]] = []
    for i, name in enumerate(ordered):
        a = _attrs(graph, name)
        label = _label(a, name)
        start = a.get("start_line")
        nid = f"n{i}"
        id_of[name] = nid
        nodes.append(
            {
                "id": nid,
                "name": name,
                "label": label,
                "short": _short(label),
                "file": a.get("file"),
                "line": (start + 1) if isinstance(start, int) else None,
                "end_line": (
                    (a.get("end_line") + 1)
                    if isinstance(a.get("end_line"), int)
                    else None
                ),
                "kind": a.get("type") or "symbol",
                "depth": 0 if name in seeds else 1,
                "is_root": name in seeds,  # highlight the page's own symbols
                "external": _is_external(a, repo_dir),
            }
        )

    edges: List[Dict[str, Any]] = []
    for s, t in edge_order:
        if s not in id_of or t not in id_of:
            continue
        edge: Dict[str, Any] = {"source": id_of[s], "target": id_of[t]}
        anchors = edge_anchors.get((s, t))
        if anchors:
            edge["anchors"] = anchors
        edges.append(edge)

    nodes, edges = _enrich(graph, nodes, edges)
    hierarchy = (
        hierarchy_for_view(hierarchy_graph, nodes)
        if hierarchy_graph is not None
        else hierarchy_from_view_nodes(nodes)
    )
    edges = attach_edge_routes(edges, hierarchy)
    return {
        "available": True,
        "root": "",
        "root_label": "",
        "direction": "both",
        "depth": 1,
        "truncated": len(names) >= max_nodes,
        "nodes": nodes,
        "edges": edges,
        "hierarchy": hierarchy,
        "mermaid": "",
        "note": "",
    }
