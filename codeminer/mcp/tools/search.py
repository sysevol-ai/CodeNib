"""
Search tools for MCP server.

Provides semantic, BM25, and regex search over code indexes.
"""

import asyncio
from typing import Optional

from ...log_utils import get_logger
from ..context import ServerContext

logger = get_logger(__name__)


async def search_semantic(
    ctx: ServerContext,
    query: str,
    top_k: int = 10,
    level: Optional[str] = None,
    score_threshold: Optional[float] = None,
    transform: Optional[str] = None,  # reserved for Phase 3 HyDE/expand
) -> list[dict]:
    """
    Semantic code search using vector embeddings.

    Args:
        ctx: ServerContext with loaded indexes
        query: Natural language or code query
        top_k: Number of results to return (default 10)
        level: Index level - "l0" (file) or "l2" (function), default "l2"
        score_threshold: Minimum similarity score (optional)
        transform: Reserved for query transformation (HyDE/expand), no-op for now

    Returns:
        List of NodeInfo dicts with scores and content

    Example:
        >>> results = await search_semantic(
        ...     ctx, query="find authentication functions", top_k=5
        ... )
        >>> results[0]["node_name"]
        'src/auth.py::authenticate_user'
    """
    # Check if vector store is loaded
    if ctx.vector is None:
        return {
            "error": "Vector index not loaded. Re-run indexing with embedding enabled."
        }

    # Default level to "l2" (function-level) if not specified
    if level is None:
        level = "l2"

    # Call underlying CodeVectorStore (sync, need to wrap)
    results = await asyncio.to_thread(
        ctx.vector.search_with_content,
        query=query,
        top_k=top_k,
        level=level,
        score_threshold=score_threshold,
    )

    # Convert NodeInfo objects to dicts for JSON serialization
    return [node.model_dump() for node in results]
