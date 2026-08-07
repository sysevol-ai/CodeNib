# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency / impact analysis over the code call-graph.

This is the structural-analysis use of the code graph — the regime where there
is *no grep substitute*: a human (or agent) cannot eyeball-grep a transitive
call closure. It answers the questions the graph is uniquely good at:

  - ``impact(symbol)``       — who is (transitively) affected if I change X?
                               (the blast radius — transitive callers)
  - ``dependencies(symbol)`` — what does X (transitively) rely on?
                               (transitive callees)
  - ``call_path(a, b)``      — how does control flow from A to B?
  - ``subgraph(symbol)``     — a compact neighborhood as nodes+edges, ready to
                               render in a frontend dependency view.

Built on :class:`RepoDependencySearcher` (1-hop, edge-typed) + BFS, and on
:meth:`CodeGraph.resolve_symbol` so callers pass a readable symbol
(``popGenericCommand``) rather than a content-hash identity key. Results are
compact and readable; callers ``file_read`` the few they care about.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ..types import EDGE_TYPE_REFERENCE, is_symbol_node, node_is_reference_only
from .code_graph import CodeGraph
from .traverse_graph import RepoDependencySearcher

# Call/use edges (X references Y). The other edge type, ``contain`` (file→symbol
# nesting), is structural, not a dependency — excluded from impact by default.
_REFERENCE_EDGES = {EDGE_TYPE_REFERENCE}


@dataclass
class DepNode:
    """A node in a dependency result (readable label, location, BFS depth)."""

    name: str  # readable unified_name (or canonical when none)
    file: Optional[str]
    line: Optional[int]
    kind: str
    depth: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "depth": self.depth,
        }


@dataclass
class DepEdge:
    source: str  # readable
    target: str  # readable
    type: str

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "target": self.target, "type": self.type}


@dataclass
class DependencyResult:
    """Structured, frontend-ready dependency/impact result."""

    root: str  # readable label of the queried symbol
    direction: str  # "callers" | "callees" | "both"
    nodes: List[DepNode] = field(default_factory=list)
    edges: List[DepEdge] = field(default_factory=list)
    truncated: bool = False
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "direction": self.direction,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "truncated": self.truncated,
            "note": self.note,
        }

    def files(self) -> List[str]:
        """Distinct files touched, nearest-first (by min BFS depth)."""
        best: Dict[str, int] = {}
        for n in self.nodes:
            if n.file and (n.file not in best or n.depth < best[n.file]):
                best[n.file] = n.depth
        return [f for f, _ in sorted(best.items(), key=lambda kv: kv[1])]


class DependencyAnalyzer:
    """Impact / dependency / path / subgraph queries over a :class:`CodeGraph`."""

    def __init__(self, code_graph: CodeGraph):
        self.code_graph = code_graph
        self.searcher = RepoDependencySearcher(code_graph)

    # -- public API ----------------------------------------------------

    def impact(
        self, symbol: str, max_depth: int = 3, max_nodes: int = 100
    ) -> DependencyResult:
        """Blast radius: everything that (transitively) CALLS *symbol*.

        "If I change this function, what might break?" — transitive callers.
        """
        return self._bfs(symbol, "backward", "callers", max_depth, max_nodes)

    def dependencies(
        self, symbol: str, max_depth: int = 3, max_nodes: int = 100
    ) -> DependencyResult:
        """What *symbol* (transitively) depends on — transitive callees."""
        return self._bfs(symbol, "forward", "callees", max_depth, max_nodes)

    def subgraph(
        self,
        symbol: str,
        radius: int = 1,
        max_nodes: int = 200,
    ) -> DependencyResult:
        """Compact callers+callees neighborhood for a frontend dependency view."""
        return self._bfs(symbol, "both", "both", radius, max_nodes=max_nodes)

    def call_path(self, from_symbol: str, to_symbol: str) -> DependencyResult:
        """Shortest call path from *from_symbol* to *to_symbol* (empty if none)."""
        a, a_c = self.code_graph.resolve_symbol(from_symbol)
        b, b_c = self.code_graph.resolve_symbol(to_symbol)
        root = self.code_graph.display_name(a) if a else from_symbol
        res = DependencyResult(root=root, direction="path")
        if a is None:
            res.note = f"{from_symbol!r} unresolved; candidates: {a_c}"
            return res
        if b is None:
            res.note = f"{to_symbol!r} unresolved; candidates: {b_c}"
            return res
        n2v = self.code_graph.name_to_vertex
        try:
            paths = self.code_graph.graph.get_shortest_paths(
                n2v[a], to=n2v[b], mode="out"
            )
        except Exception:  # noqa: BLE001
            paths = []
        path = paths[0] if paths else []
        if not path:
            res.note = f"no call path from {root} to {self.code_graph.display_name(b)}"
            return res
        names = self._names_for_ids(list(path))
        for depth, nm in enumerate(names):
            res.nodes.append(self._node(nm, depth))
            if depth:
                res.edges.append(
                    DepEdge(
                        self.code_graph.display_name(names[depth - 1]),
                        self.code_graph.display_name(nm),
                        EDGE_TYPE_REFERENCE,
                    )
                )
        return res

    # -- internals -----------------------------------------------------

    def _bfs(
        self,
        symbol: str,
        direction: str,
        label: str,
        max_depth: int,
        max_nodes: int,
    ) -> DependencyResult:
        canonical, cands = self.code_graph.resolve_symbol(symbol)
        root = self.code_graph.display_name(canonical) if canonical else symbol
        res = DependencyResult(root=root, direction=label)
        if canonical is None:
            res.note = f"{symbol!r} unresolved" + (
                f"; candidates: {cands}" if cands else " in the code graph"
            )
            return res

        max_nodes = max(1, int(max_nodes))
        seen: Set[str] = {canonical}
        emitted_edges: Set[Tuple[str, str, str]] = set()
        queue: deque[Tuple[str, int]] = deque([(canonical, 0)])
        dirs = ["forward", "backward"] if direction == "both" else [direction]
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for d in dirs:
                _, edges = self.searcher.get_neighbors(
                    node, direction=d, etype_filter=_REFERENCE_EDGES
                )
                for src, tgt, _w, attr in edges:
                    neighbor = tgt if d == "forward" else src
                    if neighbor not in seen:
                        if len(seen) >= max_nodes:
                            res.truncated = True
                            continue
                        seen.add(neighbor)
                        res.nodes.append(self._node(neighbor, depth + 1))
                        queue.append((neighbor, depth + 1))
                    edge = (
                        self.code_graph.display_name(src),
                        self.code_graph.display_name(tgt),
                        attr.get("type", EDGE_TYPE_REFERENCE),
                    )
                    if edge not in emitted_edges:
                        emitted_edges.add(edge)
                        res.edges.append(DepEdge(*edge))
        return res

    def _node(self, canonical_name: str, depth: int) -> DepNode:
        info = self.code_graph.get_node_info_by_name(canonical_name) or {}
        has_source = not is_symbol_node(info.get("type")) or not node_is_reference_only(
            info
        )
        return DepNode(
            name=info.get("unified_name") or canonical_name,
            file=info.get("file") if has_source else None,
            line=info.get("start_line") if has_source else None,
            kind=info.get("type", ""),
            depth=depth,
        )

    def _names_for_ids(self, vids: List[int]) -> List[str]:
        out = []
        for vid in vids:
            info = self.code_graph.get_node_info_by_id(vid) or {}
            nm = info.get("name")
            if nm:
                out.append(nm)
        return out
