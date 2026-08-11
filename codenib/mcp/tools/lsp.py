# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LSP-shaped graph navigation tools for MCP clients."""

from __future__ import annotations

from typing import Any, Sequence

from ._validation import (
    MAX_LSP_POSITION,
    MAX_LSP_QUERY_CHARS,
    MAX_LSP_ROUTE_TEXT_CHARS,
    MAX_LSP_SYMBOL_CHARS,
    MAX_ROUTE_SYMBOLS,
    MAX_SOURCE_PATH_CHARS,
    MAX_TOOL_RESULTS,
    bounded_integer,
    bounded_text,
)


def _coerce_symbols(symbols: Sequence[str] | str) -> list[str]:
    if isinstance(symbols, str):
        route_text = bounded_text(
            symbols,
            name="symbols",
            maximum=MAX_LSP_ROUTE_TEXT_CHARS,
        )
        values: Sequence[str] = route_text.split(",")
    elif isinstance(symbols, Sequence):
        values = symbols
    else:
        raise ValueError("symbols must be a string or sequence of strings.")

    if len(values) > MAX_ROUTE_SYMBOLS:
        raise ValueError(f"symbols must contain at most {MAX_ROUTE_SYMBOLS} entries.")

    normalized: list[str] = []
    total_chars = 0
    for value in values:
        symbol = bounded_text(
            value,
            name="each symbol",
            maximum=MAX_LSP_SYMBOL_CHARS,
        )
        total_chars += len(symbol)
        if total_chars > MAX_LSP_ROUTE_TEXT_CHARS:
            raise ValueError(
                "symbols must not exceed "
                f"{MAX_LSP_ROUTE_TEXT_CHARS} total characters."
            )
        normalized_symbol = symbol.strip()
        if normalized_symbol:
            normalized.append(normalized_symbol)
    return normalized


def _node_to_dict(node: Any) -> dict[str, Any]:
    if not (hasattr(node, "model_dump") or isinstance(node, dict)):
        return {"node": str(node)}

    from ...agent.boundary import to_agent_repr

    return to_agent_repr(node)


def _lsp_provider(ctx: Any) -> Any:
    from ...agent.lsp_provider import resolve_lsp_provider

    try:
        return resolve_lsp_provider(ctx)
    except RuntimeError:
        return None


def _serialize_results(results: Any) -> list[dict[str, Any]]:
    from ...agent.lsp_provider import lsp_result_metadata

    metadata = lsp_result_metadata(results)
    rows = [_node_to_dict(node) for node in results]
    if metadata is not None and (
        metadata.get("backend") is not None
        or metadata.get("fallback_reason") is not None
    ):
        for row in rows:
            row["lsp_provider"] = dict(metadata)
    return rows


def lsp_definition_impl(
    ctx: Any,
    file_path: str = "",
    line: int | None = None,
    character: int | None = None,
    symbol: str = "",
    top_k: int = 8,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return provider-backed definition locations."""
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_TOOL_RESULTS)
    file_path = bounded_text(
        file_path,
        name="file_path",
        maximum=MAX_SOURCE_PATH_CHARS,
    )
    symbol = bounded_text(
        symbol,
        name="symbol",
        maximum=MAX_LSP_SYMBOL_CHARS,
    )
    if line is not None:
        bounded_integer(line, name="line", maximum=MAX_LSP_POSITION)
    if character is not None:
        bounded_integer(
            character,
            name="character",
            minimum=0,
            maximum=MAX_LSP_POSITION,
        )
    provider = _lsp_provider(ctx)
    if provider is None:
        return {"error": "symbol_graph index not available"}

    from ...agent.boundary import from_agent_repr

    graph_line = from_agent_repr(line)
    try:
        results = provider.definition(
            file_path=file_path or None,
            line=graph_line,
            character=character,
            symbol=symbol or None,
            top_k=top_k,
        )
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)}
    return _serialize_results(results)


def lsp_references_impl(
    ctx: Any,
    file_path: str = "",
    line: int | None = None,
    character: int | None = None,
    symbol: str = "",
    include_declaration: bool = True,
    top_k: int = 40,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return provider-backed reference locations."""
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_TOOL_RESULTS)
    file_path = bounded_text(
        file_path,
        name="file_path",
        maximum=MAX_SOURCE_PATH_CHARS,
    )
    symbol = bounded_text(
        symbol,
        name="symbol",
        maximum=MAX_LSP_SYMBOL_CHARS,
    )
    if line is not None:
        bounded_integer(line, name="line", maximum=MAX_LSP_POSITION)
    if character is not None:
        bounded_integer(
            character,
            name="character",
            minimum=0,
            maximum=MAX_LSP_POSITION,
        )
    provider = _lsp_provider(ctx)
    if provider is None:
        return {"error": "symbol_graph index not available"}

    from ...agent.boundary import from_agent_repr

    graph_line = from_agent_repr(line)
    try:
        results = provider.references(
            file_path=file_path or None,
            line=graph_line,
            character=character,
            symbol=symbol or None,
            include_declaration=bool(include_declaration),
            top_k=top_k,
        )
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)}
    return _serialize_results(results)


def lsp_route_impl(
    ctx: Any,
    symbols: Sequence[str] | str,
    query: str = "",
    top_k: int = 12,
    include_neighbors: bool = True,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return provider-backed route anchors for one or more symbol seeds."""
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_TOOL_RESULTS)
    seeds = _coerce_symbols(symbols)
    requested_query = bounded_text(
        query,
        name="query",
        maximum=MAX_LSP_QUERY_CHARS,
    )
    normalized_query = requested_query.strip()
    if not seeds and not normalized_query:
        return []
    provider = _lsp_provider(ctx)
    if provider is None:
        return {"error": "symbol_graph index not available"}

    try:
        results = provider.route(
            symbols=seeds,
            query=normalized_query or None,
            top_k=top_k,
            include_neighbors=bool(include_neighbors),
        )
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)}
    return _serialize_results(results)
