"""
MCP server for CodeMiner semantic search.

Exposes CodeMiner's vector search capability via Model Context Protocol.
Requires a pre-built index (run indexing first with embedding enabled).

Usage:
    python -m codeminer.mcp.server --manifest /path/to/.codeminer_cache/repo_manifest.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .context import ServerContext
from .tools.search import search_semantic
from ..log_utils import get_logger

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    FastMCP = None  # Will check later when actually running the server

logger = get_logger(__name__)

# Global context (loaded once at startup)
_ctx: ServerContext | None = None


def get_context() -> ServerContext:
    """Get the loaded ServerContext."""
    if _ctx is None:
        raise RuntimeError("ServerContext not initialized. Call init_server() first.")
    return _ctx


# Initialize FastMCP server (only if available)
if FastMCP is not None:
    mcp = FastMCP("codeminer-search")
else:
    mcp = None


def _check_mcp_available():
    """Check if MCP is available, raise error if not."""
    if FastMCP is None:
        raise ImportError(
            "FastMCP not installed. Install with: pip install 'mcp[server]'"
        )


async def semantic_search(
    query: str,
    top_k: int = 10,
    level: str | None = None,
    score_threshold: float | None = None,
) -> list[dict]:
    """
    Search codebase semantically using vector embeddings.

    Args:
        query: Natural language or code search query
        top_k: Maximum number of results to return (default: 10)
        level: Hierarchy level - "l0" (files), "l1" (top-level symbols),
               or "l2" (functions/methods). Defaults to "l2".
        score_threshold: Minimum similarity score (0.0-1.0). Results below
                        this threshold are filtered out.

    Returns:
        List of matching code nodes with metadata:
        - node_id: Unique identifier
        - file_path: Source file path
        - node_type: Symbol type (function, class, method, etc.)
        - content: Source code
        - score: Similarity score (higher = more relevant)
        - start_line, end_line: Line numbers (1-based)
    """
    ctx = get_context()
    return await search_semantic(
        ctx=ctx,
        query=query,
        top_k=top_k,
        level=level,
        score_threshold=score_threshold,
    )


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


def init_server(manifest_path: str) -> None:
    """Initialize server with manifest."""
    global _ctx

    _check_mcp_available()

    manifest_path = Path(manifest_path).resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    logger.info(f"Loading manifest from {manifest_path}")
    _ctx = ServerContext.load(str(manifest_path))
    logger.info("ServerContext loaded successfully")

    # Register tools and resources with FastMCP
    if mcp is not None:
        mcp.tool()(semantic_search)
        mcp.resource("info://status")(server_status)


def main() -> None:
    """Main entry point for MCP server."""
    parser = argparse.ArgumentParser(
        description="CodeMiner MCP Server - Semantic code search via MCP"
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to repo_manifest.json (in .codeminer_cache/)",
    )

    args = parser.parse_args()

    try:
        init_server(args.manifest)
        logger.info("Starting MCP server...")
        mcp.run()
    except Exception as e:
        logger.error(f"Failed to start server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
