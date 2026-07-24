# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CodeNib MCP server - stdio transport.

Exposes vector (semantic), BM25, regex, and Zoekt trigram search over a
pre-built CodeNib index via the Model Context Protocol.

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
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .context import ServerContext
from .prompts import CODEMINER_GUIDE
from .tools.dependency import dependency_subgraph_impl
from .tools.lsp import lsp_definition_impl, lsp_references_impl, lsp_route_impl
from .tools.search import search_bm25_impl, search_regex_impl
from .tools.search import search_semantic as _search_semantic_impl
from .tools.search import search_zoekt_impl

logger = logging.getLogger(__name__)

# Global context is set once at startup before the event loop runs tools.
_ctx: Optional[ServerContext] = None


def get_context() -> ServerContext:
    """Return the loaded ServerContext or raise if uninitialized."""
    if _ctx is None:
        raise RuntimeError("ServerContext not initialized. Call init_server() first.")
    return _ctx


mcp = FastMCP(
    "codeminer",
    instructions=(
        "CodeNib provides code search over pre-built indexes. "
        "Use search_semantic for vector/embedding similarity, "
        "search_bm25 for keyword lookups, search_regex for symbol-level "
        "pattern matching, and search_zoekt for fast trigram-based "
        "substring/regex search across raw file contents. Use "
        "lsp_definition, lsp_references, and lsp_route for graph-backed "
        "LSP-shaped symbol navigation."
    ),
)


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
)
async def semantic_search(
    query: str,
    top_k: int = 10,
    level: str = "l2",
    score_threshold: float = 0.0,
) -> list[dict[str, Any]] | dict[str, str]:
    """Semantic search over indexed code using vector embeddings.

    Returns a list of code-node dicts; on missing vector index, returns
    ``{"error": ...}`` so callers can recover gracefully.
    """
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return await _search_semantic_impl(
        ctx=_ctx,
        query=query,
        top_k=top_k,
        level=level if level else "l2",
        score_threshold=score_threshold if score_threshold > 0 else None,
    )


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
    return await asyncio.to_thread(search_bm25_impl, _ctx, query, top_k, filter_test)


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
    return await asyncio.to_thread(
        search_regex_impl,
        _ctx,
        pattern,
        top_k,
        file_glob or None,
        node_type or None,
        case_sensitive,
    )


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
    return await asyncio.to_thread(
        search_zoekt_impl,
        _ctx,
        query,
        top_k,
        file_filter or None,
    )


@mcp.tool(
    name="dependency_subgraph",
    description=(
        "Return the call-graph dependency subgraph for a symbol as nodes+edges "
        "JSON. direction='impact' = transitive callers (blast radius: what may "
        "break if you change it); 'dependencies' = transitive callees (what it "
        "relies on); 'both' = 1-hop caller+callee neighborhood (for a dependency "
        "view). The structural 'who calls X / what does X reach' question that "
        "grep/keyword search cannot answer; backs impact analysis and dependency "
        "visualization. Symbols are fuzzy-matched; unresolved names return a note."
    ),
)
async def dependency_subgraph(
    symbol: str,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 60,
) -> dict[str, Any]:
    """Call-graph dependency/impact subgraph for *symbol* (nodes+edges JSON)."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return await asyncio.to_thread(
        dependency_subgraph_impl, _ctx, symbol, direction, depth, max_nodes
    )


@mcp.tool(
    name="lsp_definition",
    description=(
        "Return compact definition locations from CodeNib's static symbol "
        "graph. Provide either symbol or file_path + 1-based line. Results are "
        "locations only; read source before finalizing."
    ),
)
async def lsp_definition(
    file_path: str = "",
    line: int | None = None,
    character: int | None = None,
    symbol: str = "",
    top_k: int = 8,
) -> list[dict[str, Any]] | dict[str, str]:
    """Graph-backed definition lookup over the static symbol graph."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return await asyncio.to_thread(
        lsp_definition_impl,
        _ctx,
        file_path,
        line,
        character,
        symbol,
        top_k,
    )


@mcp.tool(
    name="lsp_references",
    description=(
        "Return compact definition/reference locations from CodeNib's static "
        "symbol graph. Provide either symbol or file_path + 1-based line. "
        "Results are locations only; read source before finalizing."
    ),
)
async def lsp_references(
    file_path: str = "",
    line: int | None = None,
    character: int | None = None,
    symbol: str = "",
    include_declaration: bool = True,
    top_k: int = 40,
) -> list[dict[str, Any]] | dict[str, str]:
    """Graph-backed reference lookup over the static symbol graph."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return await asyncio.to_thread(
        lsp_references_impl,
        _ctx,
        file_path,
        line,
        character,
        symbol,
        include_declaration,
        top_k,
    )


@mcp.tool(
    name="lsp_route",
    description=(
        "Return compact route anchors from CodeNib's static symbol graph for "
        "one or more symbol seeds. Use this when multiple symbols need a route "
        "map across endpoint, bridge/factory, provider/value, or type anchors. "
        "Results are locations only; read source before finalizing."
    ),
)
async def lsp_route(
    symbols: list[str],
    query: str = "",
    top_k: int = 12,
    include_neighbors: bool = True,
) -> list[dict[str, Any]] | dict[str, str]:
    """Graph-backed route map over the static symbol graph."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return await asyncio.to_thread(
        lsp_route_impl,
        _ctx,
        symbols,
        query,
        top_k,
        include_neighbors,
    )


@mcp.tool(
    name="get_manifest",
    description=(
        "Return metadata about the indexed repository: path, commit, "
        "languages, available indexes, and capabilities."
    ),
)
async def get_manifest() -> dict[str, Any]:
    """Return the repo manifest as a dict."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return _ctx.manifest.to_dict()


# ------------------------------------------------------------------
# Prompt resource
# ------------------------------------------------------------------


@mcp.prompt(
    name="codeminer-guide",
    description="Guidance on how to use CodeNib search tools effectively.",
)
async def codeminer_guide() -> str:
    return CODEMINER_GUIDE


# ------------------------------------------------------------------
# Status (non-tool helper, used by tests and debugging)
# ------------------------------------------------------------------


def server_status() -> str:
    """Get server status and loaded indexes as readable text."""
    try:
        ctx = get_context()
        lines = [
            f"Repo: {ctx.manifest.repo_path}",
            f"Commit: {(ctx.manifest.commit or '')[:8]}",
            f"Languages: {', '.join(ctx.manifest.languages)}",
            "",
            "Indexes:",
        ]

        if ctx.vector is not None:
            stats = ctx.vector.get_stats()
            lines.append(
                f"  ✓ vector: {ctx.vector.embedding_model} "
                f"({stats['total_documents']} docs)"
            )
        else:
            lines.append("  ✗ vector: not_loaded")

        if ctx.bm25 is not None:
            lines.append("  ✓ bm25: loaded")
        else:
            lines.append("  ✗ bm25: not_loaded")

        if ctx.symbol_graph is not None:
            lines.append("  ✓ symbol_graph: loaded")
        else:
            lines.append("  ✗ symbol_graph: not_loaded")

        if ctx.zoekt is not None:
            lines.append(f"  ✓ zoekt: port={ctx.zoekt.port}")
        else:
            lines.append("  ✗ zoekt: not_loaded")

        return "\n".join(lines)
    except Exception as e:
        return f"Error getting status: {e}"


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codeminer-mcp",
        description="Start the CodeNib MCP server (stdio transport).",
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


def init_server(manifest_path: str | Path) -> None:
    """Initialize the global ServerContext from a manifest file.

    Loads the manifest and hydrates all available indexes into the
    module-level ``_ctx``. Safe to call from tests with a temporary
    manifest path.

    Raises:
        FileNotFoundError: if ``manifest_path`` does not exist.
    """
    global _ctx
    resolved = Path(manifest_path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Manifest not found: {resolved}")
    logger.info("Loading manifest from %s", resolved)
    _ctx = ServerContext.load(resolved)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: ``codeminer-mcp <manifest>``."""
    args = _parse_args(argv)
    manifest_path = args.manifest_flag or args.manifest
    if not manifest_path:
        logger.error(
            "No manifest provided. Use: codeminer-mcp <manifest> or --manifest <path>"
        )
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        init_server(manifest_path)
        logger.info("Starting MCP server on stdio...")
        mcp.run(transport="stdio")
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.error("Failed to start server: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
