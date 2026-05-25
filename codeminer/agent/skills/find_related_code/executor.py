# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent-friendly graph navigation: callers / callees of a symbol.

Unlike the low-level ``graph_expand`` (which needed exact qualified
``node_name`` seeds, exposed 8 traversal knobs, and returned full code
bodies), this skill is shaped around the question an agent actually has —
"what calls / is called by this function?" — so it composes naturally with
grep/read:

  * seed by a plain ``symbol`` name (fuzzy-resolved; ambiguity returns
    candidate names instead of failing),
  * ``relation`` is a plain enum (callers / callees / both),
  * results are a COMPACT relationship map (name, file:line, kind, relation)
    with NO code bodies — the agent uses ``file_read`` to fetch source for the
    one or two it cares about. Cheap to navigate, code retrieval deferred.
"""

from __future__ import annotations

from typing import Any, Callable, List


def _candidates(graph: Any, symbol: str, limit: int = 8) -> List[str]:
    n2v = getattr(graph, "name_to_vertex", {}) or {}
    s = (symbol or "").strip().strip("`'\"")
    if not s:
        return []
    if s in n2v:
        return [s]
    base = s.split(".")[-1].split(":")[-1]
    # qualified-suffix match (e.g. "doWatch" -> "pkg.mod.doWatch")
    suf = [k for k in n2v if k.endswith("." + s) or k.split(".")[-1] == base]
    if suf:
        return suf[:limit]
    sub = [k for k in n2v if base and base.lower() in k.lower()]
    return sub[:limit]


def _resolve(graph: Any, symbol: str):
    n2v = getattr(graph, "name_to_vertex", {}) or {}
    if symbol in n2v:
        return symbol
    cands = _candidates(graph, symbol)
    return cands[0] if len(cands) == 1 else None


def create_executor(context: Any) -> Callable[..., List[Any]]:
    """Factory: returns a callable listing callers/callees of a symbol."""
    from ....types import QueriedNode

    def execute(
        symbol: str,
        relation: str = "both",
        hops: int = 1,
        **kwargs: Any,
    ) -> List[Any]:
        graph = context.code_graph
        if graph is None:
            raise RuntimeError("Symbol graph not available")

        name = _resolve(graph, symbol)
        if name is None:
            cands = _candidates(graph, symbol)
            if cands:
                raise ValueError(
                    f"symbol {symbol!r} is ambiguous; candidates: {cands}. "
                    "Call again with one of these exact names."
                )
            raise ValueError(
                f"symbol {symbol!r} not found in the code graph. Use a name "
                "from a search result, or grep with file_search to find it."
            )

        relation = relation if relation in ("callers", "callees", "both") else "both"
        hops = max(1, min(int(hops or 1), 2))

        results: List[Any] = []
        seen = {name}
        frontier = [name]
        for _ in range(hops):
            nxt: List[str] = []
            for nm in frontier:
                pairs = []
                if relation in ("callees", "both"):
                    pairs += [(vid, "callee") for vid in graph.get_successors(nm)]
                if relation in ("callers", "both"):
                    pairs += [(vid, "caller") for vid in graph.get_predecessors(nm)]
                for vid, rel in pairs:
                    info = graph.get_node_info_by_id(vid) or {}
                    nn = info.get("name")
                    if not nn or nn in seen:
                        continue
                    seen.add(nn)
                    nxt.append(nn)
                    f = info.get("file")
                    results.append(
                        QueriedNode(
                            node_name=nn,
                            type=info.get("type", ""),
                            file=f,
                            start_line=info.get("start_line"),
                            end_line=info.get("end_line"),
                            node_id=f"{f}:{nn}" if f else nn,
                            score=1.0,
                            content=f"{rel} of {nm}",  # relation marker, no body
                        )
                    )
            frontier = nxt

        top_k = int(kwargs.get("top_k") or 40)
        return results[:top_k]

    return execute
