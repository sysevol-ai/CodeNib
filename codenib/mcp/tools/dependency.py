# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency / impact subgraph tool (#133, #19).

Exposes the call-graph's structural-analysis value over MCP: given a symbol,
return its dependency subgraph as frontend-ready nodes+edges JSON — the
"who calls X / what breaks if I change X" question grep cannot answer, and the
backing data for a DeepWiki-style dependency view.
"""

from __future__ import annotations

from typing import Any, Dict

from ...agent.boundary import AGENT_LINE_OFFSET
from ._validation import MAX_GRAPH_DEPTH, MAX_GRAPH_NODES, bounded_int, bounded_text


def dependency_subgraph_impl(
    ctx: Any,
    symbol: str,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 60,
) -> Dict[str, Any]:
    """Return ``DependencyAnalyzer`` output as a JSON dict.

    direction: ``impact`` (transitive callers / blast radius), ``dependencies``
    (transitive callees), or ``both`` (1-hop caller+callee neighborhood, for a
    dependency view). Returns ``{"error": ...}`` when the symbol graph is
    unavailable, so callers can recover gracefully.
    """
    symbol = bounded_text(symbol, name="symbol")
    depth = bounded_int(
        depth,
        name="depth",
        maximum=MAX_GRAPH_DEPTH,
    )
    max_nodes = bounded_int(
        max_nodes,
        name="max_nodes",
        maximum=MAX_GRAPH_NODES,
    )
    graph = getattr(ctx, "symbol_graph", None)
    if graph is None:
        return {"error": "symbol_graph index not available"}

    from ...graph.dependency import DependencyAnalyzer

    analyzer = DependencyAnalyzer(graph)
    d = (direction or "both").strip().lower()
    aliases = {
        "impact": "impact",
        "callers": "impact",
        "dependencies": "dependencies",
        "callees": "dependencies",
        "both": "both",
    }
    resolved_direction = aliases.get(d)
    if resolved_direction is None:
        raise ValueError("direction must be 'impact', 'dependencies', or 'both'.")
    if resolved_direction == "impact":
        result = analyzer.impact(symbol, max_depth=depth, max_nodes=max_nodes)
    elif resolved_direction == "dependencies":
        result = analyzer.dependencies(symbol, max_depth=depth, max_nodes=max_nodes)
    else:
        result = analyzer.subgraph(symbol, radius=depth, max_nodes=max_nodes)
    payload = result.to_dict()
    for node in payload["nodes"]:
        line = node.get("line")
        if isinstance(line, int) and not isinstance(line, bool):
            node["line"] = line + AGENT_LINE_OFFSET
    return payload
