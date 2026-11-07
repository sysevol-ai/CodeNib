# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded navigation over indexed static references between symbols."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import json

from ...agent.boundary import AGENT_LINE_OFFSET
from ...types import EDGE_TYPE_REFERENCE
from ._validation import (
    MAX_CHAIN_DEPTH,
    MAX_CHAINS,
    MAX_DEPENDENCY_EDGES,
    MAX_GRAPH_DEPTH,
    MAX_TOOL_RESULTS,
    bounded_integer,
    required_text,
)

MAX_DEPENDENCY_PAYLOAD_BYTES = 36 * 1024


def _measure_payload_bytes(payload: Dict[str, Any]) -> int:
    """Return serialized indented UTF-8 size of an MCP tool payload."""
    try:
        import pydantic_core

        return len(pydantic_core.to_json(payload, indent=2))
    except (ImportError, TypeError):
        return len(json.dumps(payload, indent=2).encode("utf-8"))


def bound_dependency_payload(
    payload: Dict[str, Any],
    *,
    max_payload_bytes: int = MAX_DEPENDENCY_PAYLOAD_BYTES,
) -> Dict[str, Any]:
    """Bound dependency subgraph response to fit within client delivery limits.

    Claude Code and MCP hosts offload tool results exceeding 50,000 characters
    into disk files, preventing LLMs from seeing graph relationships directly.
    When a subgraph with dense edges exceeds *max_payload_bytes*, this helper
    preserves the queried root and high-priority direct (radius-1) edges first,
    omitting distant indirect cross-edges and dangling nodes, while reporting
    exact counts in a structured response_projection block.
    """
    if max_payload_bytes <= 0 or _measure_payload_bytes(payload) <= max_payload_bytes:
        return payload

    root = payload.get("root", "")
    nodes = list(payload.get("nodes") or [])
    edges = list(payload.get("edges") or [])
    original_node_count = len(nodes)
    original_edge_count = len(edges)

    # Prioritize direct edges (connected to root) over indirect cross-edges
    direct = [e for e in edges if e.get("source") == root or e.get("target") == root]
    indirect = [
        e for e in edges if e.get("source") != root and e.get("target") != root
    ]
    candidates = direct + indirect

    low = 0
    high = len(candidates)
    best_k = 0
    while low <= high:
        mid = (low + high) // 2
        sub_edges = candidates[:mid]
        active = {root} | {e.get("source") for e in sub_edges} | {e.get("target") for e in sub_edges}
        sub_nodes = [n for n in nodes if n.get("name") in active]
        returned_indirect = sum(
            1 for e in sub_edges if e.get("source") != root and e.get("target") != root
        )
        trial_projection = {
            "reason": "response_budget",
            "original_nodes": original_node_count,
            "returned_nodes": len(sub_nodes),
            "original_edges": original_edge_count,
            "returned_edges": len(sub_edges),
            "indirect_edges_omitted": len(indirect) - returned_indirect,
            "max_payload_bytes": max_payload_bytes,
        }
        trial = dict(
            payload,
            nodes=sub_nodes,
            edges=sub_edges,
            truncated=True,
            response_projection=trial_projection,
        )
        if _measure_payload_bytes(trial) <= max_payload_bytes:
            best_k = mid
            low = mid + 1
        else:
            high = mid - 1

    sub_edges = candidates[:best_k]
    active = {root} | {e.get("source") for e in sub_edges} | {e.get("target") for e in sub_edges}
    sub_nodes = [n for n in nodes if n.get("name") in active]

    bounded = dict(payload)
    bounded["nodes"] = sub_nodes
    bounded["edges"] = sub_edges
    bounded["truncated"] = True
    returned_indirect = sum(
        1 for e in sub_edges if e.get("source") != root and e.get("target") != root
    )
    bounded["response_projection"] = {
        "reason": "response_budget",
        "original_nodes": original_node_count,
        "returned_nodes": len(sub_nodes),
        "original_edges": original_edge_count,
        "returned_edges": len(sub_edges),
        "indirect_edges_omitted": len(indirect) - returned_indirect,
        "max_payload_bytes": max_payload_bytes,
    }
    return bounded


def dependency_subgraph_impl(
    ctx: Any,
    symbol: str,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 60,
    max_edges: int = 400,
    max_payload_bytes: Optional[int] = None,
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
    response = {
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
    limit = (
        MAX_DEPENDENCY_PAYLOAD_BYTES
        if max_payload_bytes is None
        else max(0, int(max_payload_bytes))
    )
    return bound_dependency_payload(response, max_payload_bytes=limit)


def entry_chains_impl(
    ctx: Any,
    symbol: str,
    max_depth: int = 8,
    max_chains: int = 3,
) -> Dict[str, Any]:
    """Return static reference chains from entry symbols down to *symbol*.

    Each chain starts at a function, method or class that no indexed code
    references and ends at *symbol*; every later step names the line where the
    previous step references it. Returns ``{"error": ...}`` without a graph.
    """
    symbol = required_text(symbol, name="symbol")
    max_depth = bounded_integer(max_depth, name="max_depth", maximum=MAX_CHAIN_DEPTH)
    max_chains = bounded_integer(max_chains, name="max_chains", maximum=MAX_CHAINS)
    graph = getattr(ctx, "symbol_graph", None)
    if graph is None:
        return {"error": "symbol_graph index not available"}

    from ...graph.dependency import DependencyAnalyzer

    payload = (
        DependencyAnalyzer(graph)
        .entry_chains(symbol, max_depth=max_depth, max_chains=max_chains)
        .to_dict()
    )
    for chain in payload["chains"]:
        for step in chain["steps"]:
            for location in (step, step.get("referenced_at") or {}):
                line = location.get("line")
                if isinstance(line, int) and not isinstance(line, bool):
                    location["line"] = line + AGENT_LINE_OFFSET
    return payload


__all__ = [
    "MAX_DEPENDENCY_PAYLOAD_BYTES",
    "bound_dependency_payload",
    "dependency_subgraph_impl",
    "entry_chains_impl",
]
