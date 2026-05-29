# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
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

from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

from ..graph.code_graph import CodeGraph
from ..graph.traverse_graph import RepoDependencySearcher

# Node types eligible as a focus / default seed (skip file/dir/import nodes).
_SYMBOL_TYPES = frozenset({"function", "method", "class"})
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


def build_codemap(
    graph: CodeGraph,
    symbol: Optional[str] = None,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 40,
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
            "mermaid": "",
            "note": "This repo has no symbol graph.",
        }

    searcher = RepoDependencySearcher(graph)
    mode = _DIR_TO_MODE[direction]

    seen: Set[str] = {root}
    depth_of: Dict[str, int] = {root: 0}
    raw_edges: List[Tuple[str, str]] = []
    edge_seen: Set[Tuple[str, str]] = set()
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
        for src, tgt, _w, _meta in n_edges:
            key = (src, tgt)
            if key not in edge_seen:
                edge_seen.add(key)
                raw_edges.append(key)
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
                "kind": a.get("type") or "symbol",
                "depth": depth_of.get(name, 0),
                "is_root": name == root,
            }
        )

    edges = [
        {"source": id_of[s], "target": id_of[t]}
        for s, t in raw_edges
        if s in id_of and t in id_of
    ]

    return {
        "available": True,
        "root": root,
        "root_label": _label(_attrs(graph, root), root),
        "direction": direction,
        "depth": depth,
        "truncated": truncated,
        "nodes": nodes,
        "edges": edges,
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
    lines.append("  classDef root fill:#2d7ff9,stroke:#1b5fd0,color:#fff;")
    lines.append(f"  class {root_id} root;")
    return "\n".join(lines)
