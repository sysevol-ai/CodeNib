# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LSP-shaped graph navigation tools for MCP clients."""

from __future__ import annotations

from typing import Any, Sequence

from ._validation import MAX_ROUTE_SYMBOLS, bounded_int, bounded_text


def _coerce_symbols(symbols: Sequence[str] | str) -> list[str]:
    if isinstance(symbols, str):
        return [s.strip() for s in symbols.split(",") if s.strip()]
    return [str(s).strip() for s in symbols or [] if str(s).strip()]


def _node_to_dict(node: Any) -> dict[str, Any]:
    if not (hasattr(node, "model_dump") or isinstance(node, dict)):
        return {"node": str(node)}

    from ...agent.boundary import to_agent_repr

    return to_agent_repr(node)


def _symbol_graph(ctx: Any) -> Any:
    graph = getattr(ctx, "symbol_graph", None)
    if graph is None:
        return None
    return graph


def lsp_definition_impl(
    ctx: Any,
    file_path: str = "",
    line: int | None = None,
    character: int | None = None,
    symbol: str = "",
    top_k: int = 8,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return graph-backed definition locations."""
    graph = _symbol_graph(ctx)
    if graph is None:
        return {"error": "symbol_graph index not available"}

    from ...agent.boundary import from_agent_repr
    from ...agent.lsp_provider import StaticLSPProvider

    top_k = bounded_int(top_k, name="top_k")
    graph_line = from_agent_repr(line)
    try:
        results = StaticLSPProvider(graph).definition(
            file_path=file_path or None,
            line=graph_line,
            character=character,
            symbol=symbol or None,
            top_k=top_k,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return [_node_to_dict(node) for node in results]


def lsp_references_impl(
    ctx: Any,
    file_path: str = "",
    line: int | None = None,
    character: int | None = None,
    symbol: str = "",
    include_declaration: bool = True,
    top_k: int = 40,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return graph-backed reference locations."""
    graph = _symbol_graph(ctx)
    if graph is None:
        return {"error": "symbol_graph index not available"}

    from ...agent.boundary import from_agent_repr
    from ...agent.lsp_provider import StaticLSPProvider

    top_k = bounded_int(top_k, name="top_k")
    graph_line = from_agent_repr(line)
    try:
        results = StaticLSPProvider(graph).references(
            file_path=file_path or None,
            line=graph_line,
            character=character,
            symbol=symbol or None,
            include_declaration=bool(include_declaration),
            top_k=top_k,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return [_node_to_dict(node) for node in results]


def lsp_route_impl(
    ctx: Any,
    symbols: Sequence[str] | str,
    query: str = "",
    top_k: int = 12,
    include_neighbors: bool = True,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return graph-backed route anchors for one or more symbol seeds."""
    graph = _symbol_graph(ctx)
    if graph is None:
        return {"error": "symbol_graph index not available"}

    from ...agent.lsp_provider import StaticLSPProvider

    top_k = bounded_int(top_k, name="top_k")
    seeds = _coerce_symbols(symbols)
    if not seeds:
        return []
    if len(seeds) > MAX_ROUTE_SYMBOLS:
        raise ValueError(f"symbols must contain at most {MAX_ROUTE_SYMBOLS} entries.")
    seeds = [bounded_text(seed, name="symbol") for seed in seeds]
    results = StaticLSPProvider(graph).route(
        symbols=seeds,
        query=query or None,
        top_k=top_k,
        include_neighbors=bool(include_neighbors),
    )
    return [_node_to_dict(node) for node in results]
