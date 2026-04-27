"""Search MCP tools - BM25 and regex wrappers over backbone indexes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...types import NodeInfo


def _node_to_dict(node: NodeInfo) -> Dict[str, Any]:
    """Serialize a NodeInfo to a JSON-friendly dict, dropping None fields."""
    d = node.model_dump(exclude_none=True)
    return d


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
                "Requires a loaded symbol_graph (no 'symbol_graph' entry in manifest or load failed).",
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
