# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LSP-shaped graph navigation tools for MCP clients."""

from __future__ import annotations

from typing import Any, Sequence

MAX_LSP_RESULTS = 100
MAX_LSP_ROUTE_SYMBOLS = 100
MAX_LSP_PATH_CHARS = 4_096
MAX_LSP_SYMBOL_CHARS = 1_024
MAX_LSP_QUERY_CHARS = 16_000
MAX_LSP_ROUTE_TEXT_CHARS = 16_000


def _validate_top_k(top_k: int) -> int:
    if (
        isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or not 1 <= top_k <= MAX_LSP_RESULTS
    ):
        raise ValueError("top_k must be an integer between 1 and 100")
    return top_k


def _validate_text(value: str, *, name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > max_chars:
        raise ValueError(f"{name} must contain at most {max_chars} characters")
    return value


def _coerce_symbols(symbols: Sequence[str] | str) -> list[str]:
    if isinstance(symbols, str):
        _validate_text(
            symbols,
            name="symbols",
            max_chars=MAX_LSP_ROUTE_TEXT_CHARS,
        )
        values = symbols.split(",")
    else:
        values = symbols or []

    normalized: list[str] = []
    for value in values:
        seed = str(value).strip()
        if not seed:
            continue
        _validate_text(
            seed,
            name="each symbol",
            max_chars=MAX_LSP_SYMBOL_CHARS,
        )
        normalized.append(seed)
        if len(normalized) > MAX_LSP_ROUTE_SYMBOLS:
            raise ValueError(
                "symbols must contain at most "
                f"{MAX_LSP_ROUTE_SYMBOLS} non-empty entries"
            )
    return normalized


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

    graph_line = from_agent_repr(line)
    try:
        limit = _validate_top_k(top_k)
        requested_path = _validate_text(
            file_path,
            name="file_path",
            max_chars=MAX_LSP_PATH_CHARS,
        )
        requested_symbol = _validate_text(
            symbol,
            name="symbol",
            max_chars=MAX_LSP_SYMBOL_CHARS,
        )

        from ...agent.lsp_provider import StaticLSPProvider

        results = StaticLSPProvider(graph).definition(
            file_path=requested_path or None,
            line=graph_line,
            character=character,
            symbol=requested_symbol or None,
            top_k=limit,
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

    graph_line = from_agent_repr(line)
    try:
        limit = _validate_top_k(top_k)
        requested_path = _validate_text(
            file_path,
            name="file_path",
            max_chars=MAX_LSP_PATH_CHARS,
        )
        requested_symbol = _validate_text(
            symbol,
            name="symbol",
            max_chars=MAX_LSP_SYMBOL_CHARS,
        )

        from ...agent.lsp_provider import StaticLSPProvider

        results = StaticLSPProvider(graph).references(
            file_path=requested_path or None,
            line=graph_line,
            character=character,
            symbol=requested_symbol or None,
            include_declaration=bool(include_declaration),
            top_k=limit,
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

    try:
        limit = _validate_top_k(top_k)
        seeds = _coerce_symbols(symbols)
        requested_query = _validate_text(
            query,
            name="query",
            max_chars=MAX_LSP_QUERY_CHARS,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    if not seeds:
        return []

    from ...agent.lsp_provider import StaticLSPProvider

    results = StaticLSPProvider(graph).route(
        symbols=seeds,
        query=requested_query or None,
        top_k=limit,
        include_neighbors=bool(include_neighbors),
    )
    return [_node_to_dict(node) for node in results]
