# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LSP-shaped graph navigation tools for MCP clients."""

from __future__ import annotations

from typing import Any, Sequence

from ...agent.skills._graphnav import lsp_route as graph_lsp_route


def _coerce_symbols(symbols: Sequence[str] | str) -> list[str]:
    if isinstance(symbols, str):
        return [s.strip() for s in symbols.split(",") if s.strip()]
    return [str(s).strip() for s in symbols or [] if str(s).strip()]


def _node_to_dict(node: Any) -> dict[str, Any]:
    if hasattr(node, "model_dump"):
        payload = node.model_dump(exclude_none=True)
    elif isinstance(node, dict):
        payload = {k: v for k, v in node.items() if v is not None}
    else:
        return {"node": str(node)}
    for key in ("start_line", "end_line"):
        value = payload.get(key)
        if isinstance(value, int):
            payload[key] = value + 1
    return payload


def lsp_route_impl(
    ctx: Any,
    symbols: Sequence[str] | str,
    query: str = "",
    top_k: int = 12,
    include_neighbors: bool = True,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return graph-backed route anchors for one or more symbol seeds.

    This is LSP-shaped but uses CodeMiner's static symbol graph. It returns
    compact locations only; callers should read source before finalizing.
    """
    graph = getattr(ctx, "symbol_graph", None)
    if graph is None:
        return {"error": "symbol_graph index not available"}
    seeds = _coerce_symbols(symbols)
    if not seeds:
        return []
    try:
        results = graph_lsp_route(
            graph,
            symbols=seeds,
            query=query or None,
            top_k=max(1, int(top_k or 12)),
            include_neighbors=bool(include_neighbors),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return [_node_to_dict(node) for node in results]
