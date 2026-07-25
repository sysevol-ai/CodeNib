# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Search MCP tools - vector (semantic), BM25, regex, and Zoekt wrappers
over backbone indexes."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from ...types import NodeInfo
from ..context import ServerContext


def _node_to_dict(node: NodeInfo) -> Dict[str, Any]:
    """Serialize a NodeInfo to a JSON-friendly dict, dropping None fields."""
    return node.model_dump(exclude_none=True)


# ------------------------------------------------------------------
# search_semantic (vector embedding)
# ------------------------------------------------------------------


async def search_semantic(
    ctx: ServerContext,
    query: str,
    top_k: int = 10,
    level: Optional[str] = None,
    score_threshold: Optional[float] = None,
    transform: Optional[str] = None,  # reserved for Phase 3 HyDE/expand
) -> list[dict]:
    """Semantic code search using vector embeddings.

    Args:
        ctx: ServerContext with loaded indexes
        query: Natural language or code query
        top_k: Number of results to return (default 10)
        level: Index level - "l0" (file) or "l2" (function), default "l2"
        score_threshold: Minimum similarity score (optional)
        transform: Reserved for query transformation (HyDE/expand), no-op for now

    Returns:
        List of NodeInfo dicts with scores and content. On missing index,
        returns ``{"error": ...}`` so callers can handle gracefully.
    """
    if ctx.vector is None:
        return {
            "error": "Vector index not loaded. Re-run indexing with embedding enabled."
        }

    if level is None:
        level = "l2"

    results = await asyncio.to_thread(
        ctx.vector.search_with_content,
        query=query,
        top_k=top_k,
        level=level,
        score_threshold=score_threshold,
    )

    result_dicts = []
    for node in results:
        node_dict = node.model_dump()
        if hasattr(node_dict.get("score"), "item"):
            node_dict["score"] = float(node_dict["score"].item())
        elif isinstance(node_dict.get("score"), (int, float)):
            node_dict["score"] = float(node_dict["score"])
        result_dicts.append(node_dict)
    return result_dicts


# ------------------------------------------------------------------
# search_bm25
# ------------------------------------------------------------------


def search_bm25_impl(
    ctx: Any,
    query: str,
    top_k: int = 20,
    filter_test: bool = False,
) -> List[Dict[str, Any]]:
    """Run BM25 keyword search over indexed code symbols.

    Args:
        ctx: ServerContext with a loaded ``bm25`` index.
        query: Natural-language or keyword query.
        top_k: Maximum number of results to return.
        filter_test: If True, exclude results from test files.

    Returns:
        List of dicts with keys: node_name, type, file, start_line,
        end_line, content, score.
    """
    if ctx.bm25 is None:
        raise RuntimeError(
            "BM25 index is not available. "
            + ctx.errors.get("bm25", "No 'bm25' entry in manifest or status != fresh.")
        )

    results: List[NodeInfo] = ctx.bm25.search(
        query=query,
        top_k=top_k,
        return_code_content=True,
        wrap_with_ln=False,
        filter_test=filter_test,
    )
    return [_node_to_dict(n) for n in results]


# ------------------------------------------------------------------
# search_regex
# ------------------------------------------------------------------


def search_regex_impl(
    ctx: Any,
    pattern: str,
    top_k: int = 20,
    file_glob: Optional[str] = None,
    node_type: Optional[str] = None,
    case_sensitive: bool = False,
) -> List[Dict[str, Any]]:
    """Search code graph nodes by regex pattern (grep-like).

    Args:
        ctx: ServerContext with a loaded ``regex_index``.
        pattern: Regex pattern to match against node content.
        top_k: Maximum number of results to return.
        file_glob: Optional glob to restrict by file path (e.g. ``*.py``).
        node_type: Optional filter by node type (function, class, method, file).
        case_sensitive: Whether the search is case-sensitive.

    Returns:
        List of dicts with keys: node_name, type, file, start_line,
        end_line, content.
    """
    if ctx.regex_index is None:
        raise RuntimeError(
            "Regex index is not available. "
            + ctx.errors.get(
                "regex_index",
                "Requires a loaded symbol_graph (no 'symbol_graph' entry "
                "in manifest or load failed).",
            )
        )

    try:
        results: List[NodeInfo] = ctx.regex_index.search(
            pattern=pattern,
            file_glob=file_glob,
            node_type=node_type,
            case_sensitive=case_sensitive,
            use_regex=True,
        )
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid regex pattern {pattern!r}: {exc}. "
            "See https://docs.python.org/3/library/re.html for syntax."
        ) from exc

    return [_node_to_dict(n) for n in results[:top_k]]


# ------------------------------------------------------------------
# search_zoekt
# ------------------------------------------------------------------


def search_zoekt_impl(
    ctx: Any,
    query: str,
    top_k: int = 20,
    file_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run a Zoekt trigram search and return file-level matches.

    Zoekt indexes the raw repository contents -- not the CodeGraph -- so
    results are file-level (``type="file"``) rather than the symbol-level
    ``NodeInfo`` returned by BM25 / regex.  Use this tool when you need
    fast substring or regex lookup across the whole repo (e.g. "where is
    this magic string defined", "find every occurrence of this token in
    comments").

    Args:
        ctx: ServerContext with an active ``zoekt`` searcher.
        query: Zoekt query string.  Plain substrings, regex (``r:foo``),
            and atoms like ``case:yes`` / ``lang:python`` are all valid.
        top_k: Maximum number of file matches to return.
        file_filter: Optional glob/regex appended as ``file:<expr>``.

    Returns:
        List of dicts with keys: ``node_name`` (file path), ``type``
        (``"file"``), ``file``, ``start_line``, ``end_line``, ``content``,
        ``score``, ``node_id`` (language hint, when reported).
    """
    if ctx.zoekt is None:
        raise RuntimeError(
            "Zoekt index is not available. "
            + ctx.errors.get(
                "zoekt",
                "No 'zoekt' entry in manifest, status != fresh, or "
                "zoekt-webserver could not be started. "
                "Run 'make zoekt-tool' and use 'make active-scip-env' for PATH.",
            )
        )

    from ...index.trigram import ZoektUnavailableError

    try:
        results: List[NodeInfo] = ctx.zoekt.search(
            query=query,
            top_k=top_k,
            file_filter=file_filter or None,
        )
    except ZoektUnavailableError as exc:
        raise RuntimeError(f"Zoekt search failed: {exc}") from exc

    return [_node_to_dict(n) for n in results]
