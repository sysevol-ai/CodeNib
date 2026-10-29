# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded navigation over indexed static references between symbols."""

from __future__ import annotations

from typing import Any, Dict

from ...agent.boundary import AGENT_LINE_OFFSET
from ...types import EDGE_TYPE_REFERENCE
from ._validation import (
    MAX_DEPENDENCY_EDGES,
    MAX_GRAPH_DEPTH,
    MAX_TOOL_RESULTS,
    bounded_integer,
    required_text,
)


def dependency_subgraph_impl(
    ctx: Any,
    symbol: str,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 60,
    max_edges: int = 400,
) -> Dict[str, Any]:
    """Return ``DependencyAnalyzer`` output as a JSON dict.

    ``impact`` follows incoming references, ``dependencies`` outgoing references,
    and ``both`` a neighborhood bounded by ``depth``. Legacy caller/callee
    aliases and result labels remain compatible. Reference edges can include
    calls, fields, types and function values; they do not establish invocation.
    Returns ``{"error": ...}`` when the symbol graph is unavailable.
    """
    symbol = required_text(symbol, name="symbol")
    depth = bounded_integer(depth, name="depth", maximum=MAX_GRAPH_DEPTH)
    max_nodes = bounded_integer(
        max_nodes,
        name="max_nodes",
        maximum=MAX_TOOL_RESULTS,
    )
    max_edges = bounded_integer(
        max_edges,
        name="max_edges",
        maximum=MAX_DEPENDENCY_EDGES,
    )
    direction = (direction or "").strip().lower()
    if direction in {"impact", "callers"}:
        operation = "impact"
    elif direction in {"dependencies", "dependency", "callees"}:
        operation = "dependencies"
    elif direction == "both":
        operation = "both"
    else:
        raise ValueError("direction must be 'impact', 'dependencies', or 'both'.")

    graph = getattr(ctx, "symbol_graph", None)
    if graph is None:
        return {"error": "symbol_graph index not available"}

    from ...graph.dependency import DependencyAnalyzer

    analyzer = DependencyAnalyzer(graph)
    if operation == "impact":
        result = analyzer.impact(
            symbol,
            max_depth=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    elif operation == "dependencies":
        result = analyzer.dependencies(
            symbol,
            max_depth=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    else:
        result = analyzer.subgraph(
            symbol,
            radius=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    payload = result.to_dict()
    for node in payload["nodes"]:
        line = node.get("line")
        if isinstance(line, int) and not isinstance(line, bool):
            node["line"] = line + AGENT_LINE_OFFSET
    return {
        "semantics": {
            "edge_type": EDGE_TYPE_REFERENCE,
            "reference_direction": {
                "impact": "incoming",
                "dependencies": "outgoing",
                "both": "both",
            }[operation],
            "interpretation": (
                "Indexed static references can include calls, field access, "
                "type use and function values. An edge does not prove that "
                "the target is invoked; inspect the source call site."
            ),
            "coverage": (
                "External and dynamic calls may be missing. This is not a "
                "runtime trace or a guarantee of complete call coverage."
            ),
        },
        **payload,
    }
