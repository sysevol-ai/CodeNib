"""CodeMiner MCP server - stdio transport.

Usage::

    codeminer-mcp --manifest /path/to/repo_manifest.json

Or as a module::

    python -m codeminer.mcp.server --manifest /path/to/repo_manifest.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .context import ServerContext
from .prompts import CODEMINER_GUIDE
from .tools.search import search_bm25_impl, search_regex_impl, search_zoekt_impl

logger = logging.getLogger(__name__)

# Global context is set once at startup before the event loop runs tools.
_ctx: Optional[ServerContext] = None

mcp = FastMCP(
    "codeminer",
    instructions=(
        "CodeMiner provides semantic code search over pre-built indexes. "
        "Use search_bm25 for keyword lookups, search_regex for symbol-level "
        "pattern matching, and search_zoekt for fast trigram-based substring/"
        "regex search across raw file contents."
    ),
)


# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------


@mcp.tool(
    name="search_bm25",
    description=(
        "Search for code symbols using BM25 keyword retrieval. "
        "Returns functions, classes, and methods ranked by relevance "
        "with source content. Best for exact-name or keyword lookups. "
        "Prefer this over search_regex when you know the symbol name "
        "or have a natural-language description of what the code does."
    ),
)
async def search_bm25(
    query: str,
    top_k: int = 20,
    filter_test: bool = False,
) -> list[dict[str, Any]]:
    """BM25 keyword search over indexed code symbols."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    results = await asyncio.to_thread(
        search_bm25_impl, _ctx, query, top_k, filter_test
    )
    return results


@mcp.tool(
    name="search_regex",
    description=(
        "Search code graph nodes by regex pattern (grep-like). "
        "Supports file glob filtering and node type filtering. "
        "Returns matching functions, classes, and methods with source content. "
        "Best for pattern-based searches across the codebase. "
        "Prefer search_bm25 when you know the exact name; "
        "prefer this when you need structural pattern matching."
    ),
)
async def search_regex(
    pattern: str,
    top_k: int = 20,
    file_glob: str = "",
    node_type: str = "",
    case_sensitive: bool = False,
) -> list[dict[str, Any]]:
    """Regex pattern search over code graph nodes."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    results = await asyncio.to_thread(
        search_regex_impl,
        _ctx,
        pattern,
        top_k,
        file_glob or None,
        node_type or None,
        case_sensitive,
    )
    return results


@mcp.tool(
    name="search_zoekt",
    description=(
        "Search raw repository contents using a Zoekt trigram index. "
        "Returns file-level matches with line ranges and snippet content. "
        "Best for fast substring or regex lookups that span the whole "
        "repo (magic strings, comments, identifiers across files). "
        "Prefer search_bm25 / search_regex when you want symbol-level "
        "(function/class/method) results bound to the CodeGraph."
    ),
)
async def search_zoekt(
    query: str,
    top_k: int = 20,
    file_filter: str = "",
) -> list[dict[str, Any]]:
    """Trigram-based search over raw repository contents."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    results = await asyncio.to_thread(
        search_zoekt_impl,
        _ctx,
        query,
        top_k,
        file_filter or None,
    )
    return results


@mcp.tool(
    name="get_manifest",
    description=(
        "Return metadata about the indexed repository: path, commit, "
        "languages, available indexes, and capabilities."
    ),
)
async def get_manifest() -> str:
    """Return the repo manifest as JSON."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return _ctx.manifest.to_dict()


# ------------------------------------------------------------------
# Prompt resource
# ------------------------------------------------------------------


@mcp.prompt(
    name="codeminer-guide",
    description="Guidance on how to use CodeMiner search tools effectively.",
)
async def codeminer_guide() -> str:
    return CODEMINER_GUIDE


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
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: ``codeminer-mcp --manifest <manifest_path>``."""
    global _ctx
    args = _parse_args(argv)
    manifest_path = args.manifest_flag or args.manifest
    if manifest_path is None:
        raise SystemExit("manifest path is required (use --manifest <path>)")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    logger.info("Loading manifest from %s", manifest_path)
    _ctx = ServerContext.load(manifest_path)
    logger.info("Starting MCP server (stdio)")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
