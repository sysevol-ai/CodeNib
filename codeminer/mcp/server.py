"""
MCP server for CodeMiner semantic search.

Exposes CodeMiner's vector search capability via Model Context Protocol.
Requires a pre-built index (run indexing first with embedding enabled).

Usage:
    python -m codeminer.mcp.server --manifest /path/to/.codeminer_cache/repo_manifest.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from .context import ServerContext
from .tools.search import search_semantic

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None  # Will check later when actually running the server

logger = logging.getLogger(__name__)

# Global context (loaded once at startup)
_ctx: ServerContext | None = None


def get_context() -> ServerContext:
    """Get the loaded ServerContext."""
    if _ctx is None:
        raise RuntimeError("ServerContext not initialized. Call init_server() first.")
    return _ctx


# Initialize FastMCP server (only if available)
mcp = FastMCP("codeminer") if FastMCP is not None else None


# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------


@mcp.tool(
    name="search_semantic",
    description=(
        "Search codebase semantically using vector embeddings. "
        "Returns functions, classes, and methods ranked by semantic similarity. "
        "Best for natural language queries describing functionality or code snippets."
    ),
) if mcp else lambda f: f
async def search_semantic_tool(
    query: str,
    top_k: int = 10,
    level: str = "l2",
    score_threshold: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Semantic search over indexed code using vector embeddings.

    Args:
        query: Natural language or code search query
        top_k: Maximum number of results to return (default: 10)
        level: Hierarchy level - "l0" (files), "l1" (top-level symbols),
               or "l2" (functions/methods). Default: "l2"
        score_threshold: Minimum similarity score (0.0-1.0)

    Returns:
        List of code nodes with file_path, node_type, content, score, etc.
    """
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    results = await asyncio.to_thread(
        search_semantic,
        ctx=_ctx,
        query=query,
        top_k=top_k,
        level=level if level else "l2",
        score_threshold=score_threshold if score_threshold > 0 else None,
    )
    return results


def server_status() -> str:
    """Get server status and loaded indexes."""
    try:
        ctx = get_context()
        status = {
            "repo_path": ctx.manifest.repo_path,
            "commit": ctx.manifest.commit,
            "languages": ctx.manifest.languages,
            "capabilities": ctx.manifest.capabilities,
            "indexes": {},
        }

        # Check vector index
        if ctx.vector is not None:
            stats = ctx.vector.get_stats()
            status["indexes"]["vector"] = {
                "status": "loaded",
                "model": ctx.vector.embedding_model,
                "documents": stats["total_documents"],
            }
        else:
            status["indexes"]["vector"] = {"status": "not_loaded"}

        # Format as readable text
        lines = [
            f"Repo: {status['repo_path']}",
            f"Commit: {status['commit'][:8]}",
            f"Languages: {', '.join(status['languages'])}",
            "",
            "Indexes:",
        ]

        for idx_type, idx_info in status["indexes"].items():
            if idx_info["status"] == "loaded":
                if idx_type == "vector":
                    lines.append(
                        f"  ✓ {idx_type}: {idx_info['model']} ({idx_info['documents']} docs)"
                    )
            else:
                lines.append(f"  ✗ {idx_type}: {idx_info['status']}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error getting status: {e}"


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codeminer-mcp",
        description="Start the CodeMiner MCP server (stdio transport).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        type=str,
        help="Path to repo_manifest.json produced by IndexCompiler.",
    )
    parser.add_argument(
        "--manifest",
        dest="manifest_flag",
        type=str,
        help="Path to repo_manifest.json produced by IndexCompiler.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Main entry point for MCP server."""
    global _ctx

    if mcp is None:
        logger.error("FastMCP not installed. Install with: pip install 'mcp[server]'")
        sys.exit(1)

    args = _parse_args(argv)
    manifest_path = args.manifest_flag or args.manifest

    if not manifest_path:
        logger.error("No manifest provided. Use: codeminer-mcp <manifest> or --manifest <path>")
        sys.exit(1)

    manifest_path = Path(manifest_path).resolve()
    if not manifest_path.exists():
        logger.error("Manifest not found: %s", manifest_path)
        sys.exit(1)

    try:
        logger.info("Loading manifest from %s", manifest_path)
        _ctx = ServerContext.load(manifest_path)
        logger.info("Starting MCP server on stdio...")
        mcp.run()
    except Exception as exc:
        logger.error("Failed to start server: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
