# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded dependency neighborhoods over indexed static references.

``impact`` follows incoming references, ``dependencies`` outgoing references,
and ``subgraph`` both; ``entry_chains`` walks incoming references back to
symbols that nothing indexed references. Calls, field accesses, type uses and
function values can all contribute references. Inspect source to distinguish
actual invocations; these walks establish neither runtime control flow nor
complete call coverage.

The traversal uses :class:`RepoDependencySearcher` and BFS. Existing public
caller/callee names remain aliases for reference direction. ``call_path`` is a
legacy diagnostic over directed graph paths, not a reference-only call proof.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ..types import (
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    is_symbol_node,
    node_is_reference_only,
)
from .code_graph import CodeGraph
from .traverse_graph import NeighborScanBudget, RepoDependencySearcher

# Index references (X references Y). The other edge type, ``contain`` (file→symbol
# nesting), is structural, not a dependency — excluded from impact by default.
_REFERENCE_EDGES = {EDGE_TYPE_REFERENCE}
_DEFAULT_MAX_EDGES = 1000
# Entry chains pass only through code that can call: fields and modules are
# referenced, not invoked. Class references cover constructor calls.
_CHAIN_KINDS = frozenset({NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS})
# Candidate chains compared when choosing which ones to return.
_CHAIN_OPTIONS = 200


@dataclass
class DepNode:
    """A node in a dependency result (unique label, location, BFS depth)."""

    # Usually the readable unified_name. Falls back to the canonical identity
    # when two included nodes would otherwise have the same response label.
    name: str
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
    source: str  # unique result label, usually readable unified_name
    target: str  # unique result label, usually readable unified_name
    type: str

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "target": self.target, "type": self.type}


@dataclass
class DependencyResult:
    """Structured, frontend-ready dependency/impact result."""

    root: str  # unique result label of the queried symbol
    direction: str  # Legacy "callers" / "callees" labels mean reference direction.
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


@dataclass
class ChainStep:
    """One chain symbol and its incoming reference's 1-based output location."""

    node: DepNode
    reference_file: Optional[str] = None
    reference_line: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = self.node.to_dict()
        payload.pop("depth", None)
        if self.reference_file is not None:
            payload["referenced_at"] = {
                "file": self.reference_file,
                "line": self.reference_line,
            }
        return payload


@dataclass
class CallChain:
    """Static reference chain, entry first and the queried symbol last."""

    steps: List[ChainStep] = field(default_factory=list)
    # False when no unreferenced entry was reached within the search bounds.
    complete: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "complete": self.complete,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass
class EntryChainResult:
    root: str
    chains: List[CallChain] = field(default_factory=list)
    entries_found: int = 0
    truncated: bool = False
    note: Optional[str] = None
    # Set when nothing references the symbol and the chains reach a
    # referenced method of the same name that dynamic dispatch may route to it.
    via: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "root": self.root,
            "chains": [chain.to_dict() for chain in self.chains],
            "entries_found": self.entries_found,
            "truncated": self.truncated,
        }
        if self.note:
            payload["note"] = self.note
        if self.via:
            payload["via"] = self.via
        return payload


class DependencyAnalyzer:
    """Impact / dependency / path / subgraph queries over a :class:`CodeGraph`."""

    def __init__(self, code_graph: CodeGraph):
        self.code_graph = code_graph
        self.searcher = RepoDependencySearcher(code_graph)

    # -- public API ----------------------------------------------------

    def impact(
        self,
        symbol: str,
        max_depth: int = 3,
        max_nodes: int = 100,
        max_edges: int = _DEFAULT_MAX_EDGES,
    ) -> DependencyResult:
        """Potential dependents reached through incoming index references."""
        return self._bfs(symbol, "backward", "callers", max_depth, max_nodes, max_edges)

    def dependencies(
        self,
        symbol: str,
        max_depth: int = 3,
        max_nodes: int = 100,
        max_edges: int = _DEFAULT_MAX_EDGES,
    ) -> DependencyResult:
        """Indexed dependencies reached through outgoing references."""
        return self._bfs(symbol, "forward", "callees", max_depth, max_nodes, max_edges)

    def subgraph(
        self,
        symbol: str,
        radius: int = 1,
        max_nodes: int = 200,
        max_edges: int = _DEFAULT_MAX_EDGES,
    ) -> DependencyResult:
        """Compact incoming/outgoing reference neighborhood."""
        return self._bfs(
            symbol,
            "both",
            "both",
            radius,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

    def entry_chains(
        self,
        symbol: str,
        *,
        max_depth: int = 8,
        max_chains: int = 3,
        max_visited: int = 4000,
        max_scanned_edges: int = 40000,
        follow_dispatch: bool = True,
    ) -> EntryChainResult:
        """Shortest static reference chains from entry symbols down to *symbol*.

        Walk incoming references through functions, methods and classes,
        skipping tests and reference-only externals. An entry is a symbol with
        no such referrer: the indexed code never references it, so a
        framework, a command line or the outside world calls it. The shortest
        chain comes first; each later chain adds the most symbols not yet
        shown. When no entry is reached within the bounds, the longest partial
        chains are returned instead and marked incomplete.
        """
        canonical, cands = self.code_graph.resolve_symbol(symbol, candidate_limit=None)
        if canonical is None and cands:
            # ``Class.method`` also matches that method's parameters and
            # locals; keep the candidate whose name ends with the request.
            wanted = symbol.strip().strip("`'\"").removesuffix("()")
            exact = [
                name
                for name in cands
                if name.removesuffix("()").endswith((":" + wanted, "." + wanted))
            ]
            if len(exact) == 1:
                canonical, _ = self.code_graph.resolve_symbol(exact[0])
        root = self.code_graph.display_name(canonical) if canonical else symbol
        result = EntryChainResult(root=root)
        if canonical is None:
            result.note = f"{symbol!r} unresolved" + (
                f"; candidates: {cands[:8]}"
                + (f" (+{len(cands) - 8} more)" if len(cands) > 8 else "")
                if cands
                else " in the code graph"
            )
            return result

        depth_of: Dict[str, int] = {canonical: 0}
        # caller -> (callee one step closer to the target, reference site)
        next_hop: Dict[str, Tuple[str, Optional[str], Optional[int]]] = {}
        entries: List[str] = []
        queue: deque[str] = deque([canonical])
        scan_budget = NeighborScanBudget(max_scanned_edges)
        while queue:
            node = queue.popleft()
            depth = depth_of[node]
            if depth >= max(1, int(max_depth)):
                # Not expanded, but an unreferenced symbol is still an entry.
                has_referrer = self._has_chain_referrer(node, scan_budget)
                if has_referrer or scan_budget.exhausted:
                    result.truncated = True
                else:
                    entries.append(node)
                continue
            referenced = False
            neighbors = self.searcher.iter_neighbors(
                node,
                direction="backward",
                etype_filter=_REFERENCE_EDGES,
                scan_budget=scan_budget,
            )
            for caller, (_src, _tgt, _w, attr) in neighbors:
                if caller == node:
                    continue
                info = self.code_graph.get_node_info_by_name(caller) or {}
                if info.get("type") not in _CHAIN_KINDS or node_is_reference_only(info):
                    continue
                referenced = True
                if caller in depth_of:
                    continue
                if len(depth_of) >= max_visited:
                    result.truncated = True
                    continue
                depth_of[caller] = depth + 1
                next_hop[caller] = (
                    node,
                    attr.get("anchor_file"),
                    attr.get("anchor_line"),
                )
                queue.append(caller)
            if scan_budget.exhausted:
                result.truncated = True
                break
            if not referenced and node != canonical:
                entries.append(node)

        if len(depth_of) == 1:
            result.note = (
                f"no indexed references to {root}; it may be an entry point "
                "called by a framework, a command line or external code"
            )
            same_named = self._referenced_same_named_methods(canonical)
            if not same_named:
                return result
            if follow_dispatch:
                # An override is usually called as ``self.method()``, which the
                # index records against the base method: show how that is reached.
                base = self.entry_chains(
                    same_named[0],
                    max_depth=max_depth,
                    max_chains=max_chains,
                    max_visited=max_visited,
                    max_scanned_edges=max_scanned_edges,
                    follow_dispatch=False,
                )
                if base.chains:
                    result.chains = base.chains
                    result.entries_found = base.entries_found
                    result.truncated = base.truncated
                    result.via = base.root
                    result.note = (
                        f"no indexed references to {root}; the chains reach the "
                        f"same-named {base.root}, and dynamic dispatch may run "
                        f"{root} there instead (static references cannot tell "
                        "which override runs)"
                    )
                    return result
            result.note += (
                ", or reached by dynamic dispatch through a same-named "
                "method that is referenced: " + ", ".join(same_named)
            )
            return result

        def path_to_target(name: str) -> List[str]:
            path = [name]
            while path[-1] != canonical:
                path.append(next_hop[path[-1]][0])
            return path

        complete = bool(entries)
        candidates = sorted(entries, key=lambda name: (depth_of[name], name))
        if not complete:
            # No entry within bounds: show the deepest chains that were found.
            candidates = sorted(
                (name for name in depth_of if name != canonical),
                key=lambda name: (-depth_of[name], name),
            )
        # The shortest chain first (the longest when only partial chains were
        # found). Each later chain is the one that leaves the chains already
        # shown closest to the target, so different ways of reaching it come
        # before longer detours that share a route; then the shorter chain.
        options = {name: path_to_target(name) for name in candidates[:_CHAIN_OPTIONS]}
        paths: List[List[str]] = []
        shown: Set[str] = {canonical}

        def shared_tail(path: List[str]) -> int:
            longest = 0
            for other in paths:
                common = 0
                for left, right in zip(reversed(path), reversed(other), strict=False):
                    if left != right:
                        break
                    common += 1
                longest = max(longest, common)
            return longest

        while options and len(paths) < max(1, int(max_chains)):
            start = min(
                options,
                key=lambda name: (
                    shared_tail(options[name]),
                    len(options[name]) if complete else -len(options[name]),
                    name,
                ),
            )
            path = options.pop(start)
            if paths and not set(path) - shown:
                continue
            paths.append(path)
            shown.update(path)
        labels = self._result_labels(
            [canonical, *(name for path in paths for name in path)]
        )
        result.root = labels[canonical]
        for path in paths:
            steps = []
            for index, name in enumerate(path):
                node = self._node(name, index, result_name=labels[name])
                site_file = site_line = None
                if index:
                    _callee, site_file, site_line = next_hop[path[index - 1]]
                    # Graph anchors use source's 0-based coordinates.
                    if site_line is not None:
                        site_line += 1
                steps.append(ChainStep(node, site_file, site_line))
            result.chains.append(CallChain(steps=steps, complete=complete))
        result.entries_found = len(entries)
        return result

    def _referenced_same_named_methods(
        self, canonical: str, limit: int = 3
    ) -> List[str]:
        """Same-named methods elsewhere that indexed code references.

        A method override is usually called as ``self.method()`` through its
        base class, which the index records against the base method.
        """
        info = self.code_graph.get_node_info_by_name(canonical) or {}
        if info.get("type") != NODE_TYPE_METHOD:
            return []
        own_file = info.get("file")
        display = self.code_graph.display_name(canonical).removesuffix("()")
        method = display.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
        found: List[str] = []
        for vertex in self.code_graph.graph.vs:
            name = vertex["name"]
            if name == canonical or vertex.attributes().get("type") != NODE_TYPE_METHOD:
                continue
            other = self.code_graph.display_name(name).removesuffix("()")
            if not other.endswith("." + method):
                continue
            if self._has_chain_referrer(name, NeighborScanBudget(1000)):
                found.append(name)
        # A base class in the same module is the likeliest dispatch target.
        found.sort(
            key=lambda name: (
                (self.code_graph.get_node_info_by_name(name) or {}).get("file")
                != own_file,
                self.code_graph.display_name(name),
            )
        )
        return [self.code_graph.display_name(name) for name in found[:limit]]

    def _has_chain_referrer(self, name: str, scan_budget: NeighborScanBudget) -> bool:
        neighbors = self.searcher.iter_neighbors(
            name,
            direction="backward",
            etype_filter=_REFERENCE_EDGES,
            scan_budget=scan_budget,
        )
        for caller, _edge in neighbors:
            info = self.code_graph.get_node_info_by_name(caller) or {}
            if (
                caller != name
                and info.get("type") in _CHAIN_KINDS
                and not node_is_reference_only(info)
            ):
                neighbors.close()
                return True
        return False

    def call_path(self, from_symbol: str, to_symbol: str) -> DependencyResult:
        """Legacy directed graph path; may include containment, not runtime calls."""
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
        labels = self._result_labels(names)
        res.root = labels.get(a, root)
        for depth, nm in enumerate(names):
            res.nodes.append(self._node(nm, depth, result_name=labels[nm]))
            if depth:
                res.edges.append(
                    DepEdge(
                        labels[names[depth - 1]],
                        labels[nm],
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
        max_edges: int,
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
        max_edges = max(1, int(max_edges))
        seen: Set[str] = {canonical}
        emitted_edges: Set[Tuple[str, str, str]] = set()
        discovered: List[Tuple[str, int]] = []
        edge_order: List[Tuple[str, str, str]] = []
        queue: deque[Tuple[str, int]] = deque([(canonical, 0)])
        dirs = ["forward", "backward"] if direction == "both" else [direction]
        # Allow one filtered/parallel edge per emitted relationship, plus one
        # look-ahead, while still bounding adversarial raw-edge fan-out.
        scan_budget = NeighborScanBudget(max_edges * 2 + 1)
        edge_budget_exhausted = False
        while queue and not edge_budget_exhausted:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for d in dirs:
                neighbors = self.searcher.iter_neighbors(
                    node,
                    direction=d,
                    etype_filter=_REFERENCE_EDGES,
                    scan_budget=scan_budget,
                )
                for neighbor, (src, tgt, _w, attr) in neighbors:
                    # Canonical graph identities are the only safe dedup key:
                    # ``unified_name`` is a display label and may collide.
                    edge_key = (
                        src,
                        tgt,
                        attr.get("type", EDGE_TYPE_REFERENCE),
                    )
                    if edge_key in emitted_edges:
                        continue
                    if len(emitted_edges) >= max_edges:
                        res.truncated = True
                        edge_budget_exhausted = True
                        break
                    if neighbor not in seen:
                        if len(seen) >= max_nodes:
                            res.truncated = True
                            continue
                        seen.add(neighbor)
                        discovered.append((neighbor, depth + 1))
                        queue.append((neighbor, depth + 1))
                    emitted_edges.add(edge_key)
                    edge_order.append(edge_key)
                if scan_budget.exhausted:
                    res.truncated = True
                    edge_budget_exhausted = True
                if edge_budget_exhausted:
                    neighbors.close()
                    break

        labels = self._result_labels([canonical, *(name for name, _ in discovered)])
        res.root = labels[canonical]
        res.nodes = [
            self._node(name, depth, result_name=labels[name])
            for name, depth in discovered
        ]
        res.edges = [
            DepEdge(labels[src], labels[tgt], edge_type)
            for src, tgt, edge_type in edge_order
        ]
        return res

    def _result_labels(self, canonical_names: List[str]) -> Dict[str, str]:
        """Choose unique result labels while preferring readable display names."""
        names = list(dict.fromkeys(canonical_names))
        canonical_set = set(names)
        displays = {name: self.code_graph.display_name(name) for name in names}
        display_counts: Dict[str, int] = {}
        for display in displays.values():
            display_counts[display] = display_counts.get(display, 0) + 1

        return {
            name: (
                display
                if display_counts[display] == 1
                and (display not in canonical_set or display == name)
                else name
            )
            for name, display in displays.items()
        }

    def _node(
        self,
        canonical_name: str,
        depth: int,
        result_name: Optional[str] = None,
    ) -> DepNode:
        info = self.code_graph.get_node_info_by_name(canonical_name) or {}
        has_source = not is_symbol_node(info.get("type")) or not node_is_reference_only(
            info
        )
        return DepNode(
            name=result_name or info.get("unified_name") or canonical_name,
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
