# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared call-graph navigation helpers (callers / callees / trace).

Centralises the engine so the agent skills (``find_callers`` /
``find_callees`` / ``trace``) and the MCP server tools share one
implementation. All results are COMPACT — name, file:line, kind, and a short
relation marker, with no code bodies (callers fetch source with ``read`` or
the search tools). This module is not a skill itself (no ``config.yaml``), so
``SkillLoader`` ignores it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ...types import is_symbol_node, node_is_reference_only


def _bare(s: str) -> str:
    """Strip path/qualifier prefix and a trailing ``()`` to a bare symbol token."""
    return s.split(":")[-1].split(".")[-1].rstrip("()").strip()


def display_name(graph: Any, name: str) -> str:
    """Human-readable label for a graph node.

    The canonical ``name`` is the identity key (for clang/SCIP-indexed C/C++
    this is a content hash like ``8ee8d723c4a8f670`` — useless to the agent).
    The ``unified_name`` attribute carries the real display (e.g.
    ``src/t_list.c:popGenericCommand()``). Prefer it; fall back to ``name``.
    """
    info = graph.get_node_info_by_name(name) or {}
    return info.get("unified_name") or name


def _unified_index(graph: Any) -> Dict[str, List[str]]:
    """Map readable ``unified_name`` (full + bare) -> canonical identity names.

    The prebuilt graph's persisted ``_unified_to_names`` dict is often EMPTY
    (older builder didn't serialize it), even though every vertex carries a
    ``unified_name`` attribute. Without this index, readable seeds — exactly
    the on-target symbols the embedding search returns (e.g.
    ``popGenericCommand``) — fail to resolve back to a graph vertex and never
    get expanded. So build the index from vertex attributes and cache it on
    the graph object.
    """
    cache = getattr(graph, "_gn_uni_index", None)
    if cache is not None:
        return cache
    index: Dict[str, List[str]] = {}

    def _add(key: str, name: str) -> None:
        index.setdefault(key, []).append(name)

    pre = getattr(graph, "_unified_to_names", None) or {}
    if pre:
        for disp, names in pre.items():
            for nm in names:
                _add(disp, nm)
                _add(_bare(disp), nm)
    else:
        for nm in getattr(graph, "name_to_vertex", {}) or {}:
            info = graph.get_node_info_by_name(nm) or {}
            u = info.get("unified_name")
            if u:
                _add(u, nm)
                _add(_bare(u), nm)
    try:
        graph._gn_uni_index = index
    except Exception:  # noqa: BLE001 — read-only graph object; just skip caching
        pass
    return index


def candidates(graph: Any, symbol: str, limit: int = 8) -> List[str]:
    """Canonical node names that could match *symbol*.

    Matches the canonical ``name`` first, then the readable ``unified_name``
    (full display, ``file:sym()`` suffix, bare ``sym``), then a name substring.
    Returning canonical names keeps graph ops (get_successors/predecessors)
    working even when the agent re-seeds with a readable ``unified_name``.
    """
    n2v = getattr(graph, "name_to_vertex", {}) or {}
    s = (symbol or "").strip().strip("`'\"")
    if not s:
        return []
    if s in n2v:
        return [s]

    # unified_name matching (readable display -> canonical identity name(s)).
    uni = _unified_index(graph)
    if uni:
        sbase = _bare(s)

        def _dedup(names: List[str]) -> List[str]:
            seen, out = set(), []
            for h in names:
                if h not in seen:
                    seen.add(h)
                    out.append(h)
            return out[:limit]

        for key in (s, s + "()", sbase):  # exact display, then bare token
            if key in uni:
                return _dedup(uni[key])
        # substring over the distinct full display keys
        sub: List[str] = []
        for disp, names in uni.items():
            if sbase and sbase.lower() in disp.lower():
                sub += names
        if sub:
            return _dedup(sub)

    # fallback: name-based fuzzy (languages whose canonical name is readable).
    base = s.split(".")[-1].split(":")[-1]
    suf = [k for k in n2v if k.endswith("." + s) or k.split(".")[-1] == base]
    if suf:
        return suf[:limit]
    sub = [k for k in n2v if base and base.lower() in k.lower()]
    return sub[:limit]


def resolve(graph: Any, symbol: str) -> Tuple[Optional[str], List[str]]:
    """Return (canonical_name | None, candidates). None when ambiguous/missing.

    Candidates are returned as readable display names, so an ambiguity error
    shows the agent real symbols rather than content hashes.
    """
    n2v = getattr(graph, "name_to_vertex", {}) or {}
    if symbol in n2v:
        return symbol, [display_name(graph, symbol)]
    cands = candidates(graph, symbol)
    canonical = cands[0] if len(cands) == 1 else None
    return canonical, [display_name(graph, c) for c in cands]


def _compact(graph: Any, name: str, relation: str):
    """Build a body-less QueriedNode for a graph node *name* (readable label)."""
    from ...types import QueriedNode

    info = graph.get_node_info_by_name(name) or {}
    f = info.get("file")
    start_line = info.get("start_line")
    end_line = info.get("end_line")
    if is_symbol_node(info.get("type")) and node_is_reference_only(info):
        f = None
        start_line = None
        end_line = None
    disp = info.get("unified_name") or name
    return QueriedNode(
        node_name=disp,
        type=info.get("type", ""),
        file=f,
        start_line=start_line,
        end_line=end_line,
        node_id=_node_id(f, disp),
        score=1.0,
        content=relation,  # short marker, e.g. "caller of X" — no code body
    )


def _node_id(file: Optional[str], disp: str) -> str:
    """``file:disp`` — but don't double a file prefix already in *disp*.

    A readable ``unified_name`` is often already ``file:symbol()``; prefixing
    the file again would yield ``a.c:a.c:sym()``. Skip the prefix when *disp*
    is already path-qualified (contains ``/`` or ``:``)."""
    if not file:
        return disp
    if "/" in disp or ":" in disp:
        return disp
    return f"{file}:{disp}"


def _names_for_ids(graph: Any, vids: List[int]) -> List[str]:
    out = []
    for vid in vids:
        info = graph.get_node_info_by_id(vid) or {}
        nm = info.get("name")
        if nm:
            out.append(nm)
    return out


def neighbors(graph: Any, symbol: str, relation: str, top_k: int = 40) -> List[Any]:
    """Callers (predecessors) and/or callees (successors) of *symbol*, compact.

    ``relation`` ∈ {"callers", "callees", "both"}. Raises ValueError with
    candidates when the symbol can't be resolved (so the agent can re-seed).
    """
    name, cands = resolve(graph, symbol)
    if name is None:
        if cands:
            raise ValueError(
                f"symbol {symbol!r} is ambiguous; candidates: {cands}. "
                "Call again with one exact name."
            )
        raise ValueError(
            f"symbol {symbol!r} not found in the code graph. Use a name from a "
            "search result, or grep to find it."
        )
    label = display_name(graph, name)
    results: List[Any] = []
    if relation in ("callees", "both"):
        for nm in _names_for_ids(graph, graph.get_successors(name)):
            results.append(_compact(graph, nm, f"callee of {label}"))
    if relation in ("callers", "both"):
        for nm in _names_for_ids(graph, graph.get_predecessors(name)):
            results.append(_compact(graph, nm, f"caller of {label}"))
    return results[:top_k]


def trace(
    graph: Any, from_symbol: str, to_symbol: str, max_hops: int = 10
) -> List[Any]:
    """Shortest call path from *from_symbol* to *to_symbol* (compact hops).

    Returns the ordered nodes on the path (empty if unreachable). Raises
    ValueError if either endpoint can't be resolved.
    """
    a, a_c = resolve(graph, from_symbol)
    b, b_c = resolve(graph, to_symbol)
    if a is None:
        raise ValueError(f"from_symbol {from_symbol!r} unresolved; candidates: {a_c}")
    if b is None:
        raise ValueError(f"to_symbol {to_symbol!r} unresolved; candidates: {b_c}")
    n2v = graph.name_to_vertex
    try:
        paths = graph.graph.get_shortest_paths(n2v[a], to=n2v[b], mode="out")
    except Exception:  # noqa: BLE001
        paths = []
    path = paths[0] if paths else []
    if not path or len(path) > max_hops + 1:
        return []
    names = _names_for_ids(graph, list(path))
    a_lbl, b_lbl = display_name(graph, a), display_name(graph, b)
    out = []
    for i, nm in enumerate(names):
        out.append(_compact(graph, nm, f"hop {i} on path {a_lbl} -> {b_lbl}"))
    return out
