# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CodeNib MCP server - stdio transport.

Exposes vector (semantic), BM25, regex, and Zoekt trigram search over a
pre-built CodeNib index via the Model Context Protocol.

Usage::

    codenib-mcp --manifest /path/to/repo_manifest.json

Or as a module::

    python -m codenib.mcp.server --manifest /path/to/repo_manifest.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from mcp.server import MCPServer

from ..compiler.manifest import RepoManifest
from .context import ServerContext
from .prompts import CODENIB_GUIDE
from .tools.dependency import dependency_subgraph_impl
from .tools.lsp import lsp_definition_impl, lsp_references_impl, lsp_route_impl
from .tools.search import search_bm25_impl, search_context_impl, search_regex_impl
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


mcp = MCPServer(
    "codenib",
    instructions=(
        "CodeNib provides code search over pre-built indexes. "
        "Start with search_context for planned ranked retrieval over the "
        "available lexical, dense, and structural views. Use search_semantic "
        "for direct vector/embedding similarity, "
        "search_bm25 for keyword lookups, search_regex for CodeGraph "
        "file/symbol pattern matching, and search_zoekt for fast trigram-based "
        "substring/regex search across raw file contents. Use "
        "lsp_definition, lsp_references, and lsp_route for graph-backed "
        "LSP-shaped symbol navigation."
    ),
)


# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------


@mcp.tool(
    name="search_context",
    description=(
        "Recommended ranked repository-context search. CodeNib selects and "
        "executes a deterministic BM25, dense, hybrid-RRF, or graph-expanded "
        "route from the loaded views and requested budget. Returns the selected "
        "plan, repository provenance, and source-linked results."
    ),
)
async def search_context(
    query: str,
    top_k: int = 10,
    budget: str = "balanced",
    level: str = "l2",
    filter_test: bool = False,
) -> dict[str, Any]:
    """Execute capability-aware ranked retrieval over loaded repository views."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return await asyncio.to_thread(
        search_context_impl,
        _ctx,
        query,
        top_k,
        budget,
        level,
        filter_test,
    )


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
        "Returns matching file and symbol nodes with source content. "
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
        "Prefer search_bm25 for ranked symbol results or search_regex for "
        "file/symbol results bound to the CodeGraph."
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
        "symbol seeds, or use a query alone when no reliable symbol is known. "
        "Use this for a route map across endpoint, bridge/factory, "
        "provider/value, or type anchors. "
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
    result = _ctx.manifest.to_dict()
    result["runtime"] = {
        "loaded_views": sorted(_ctx.loaded_views),
        "view_errors": dict(sorted(_ctx.errors.items())),
    }
    if _ctx.artifact is not None:
        result["artifact"] = dict(_ctx.artifact)
    return result


# ------------------------------------------------------------------
# Prompt resource
# ------------------------------------------------------------------


@mcp.prompt(
    name="codenib-guide",
    description="Guidance on how to use CodeNib search tools effectively.",
)
async def codenib_guide() -> str:
    return CODENIB_GUIDE


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

_CLI_NAMES = frozenset({"codenib-mcp"})


def _cli_program_name() -> str:
    invoked_name = Path(sys.argv[0]).name
    if invoked_name in _CLI_NAMES:
        return invoked_name
    return "codenib-mcp"


def _parse_args(
    argv: list[str] | None = None,
    *,
    prog: str | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=prog or _cli_program_name(),
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
        "--artifact",
        type=str,
        help="Verified portable context artifact directory.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        help="Exact repository checkout bound to --artifact.",
    )
    parser.add_argument(
        "--repository",
        type=str,
        help="Expected owner/repository identity for --artifact.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    return parser.parse_args(argv)


def init_server(
    manifest_path: RepoManifest | str | Path,
    *,
    artifact: dict[str, Any] | None = None,
) -> None:
    """Initialize the global ServerContext from a manifest file.

    Loads the manifest and opens all available indexes in the
    module-level ``_ctx``. Safe to call from tests with a temporary
    manifest path.

    Raises:
        FileNotFoundError: if ``manifest_path`` does not exist.
    """
    global _ctx
    if isinstance(manifest_path, RepoManifest):
        manifest = manifest_path
        logger.info(
            "Loading in-memory manifest for %s@%s",
            manifest.repo_path,
            (manifest.commit or "")[:12],
        )
    else:
        resolved = Path(manifest_path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Manifest not found: {resolved}")
        logger.info("Loading manifest from %s", resolved)
        manifest = RepoManifest.load(resolved)
    if _ctx is not None:
        _ctx.close()
    _ctx = ServerContext.load(manifest, artifact=artifact)


def main(argv: list[str] | None = None) -> None:
    """Run the ``codenib-mcp`` console entry point."""
    program_name = _cli_program_name()
    args = _parse_args(argv)
    manifest_path = args.manifest_flag or args.manifest
    if args.artifact and manifest_path:
        logger.error("Choose either a manifest or --artifact, not both")
        sys.exit(1)
    if args.artifact and not args.repo:
        logger.error("--artifact requires --repo with the exact checkout")
        sys.exit(1)
    if not args.artifact and not manifest_path:
        logger.error(
            "No context provided. Use: %s <manifest> or --artifact <dir> --repo <dir>",
            program_name,
        )
        sys.exit(1)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        if args.artifact:
            from ..artifacts import bind_context_artifact

            binding = bind_context_artifact(
                args.artifact,
                args.repo,
                expected_repository=args.repository,
            )
            artifact = binding.artifact
            init_server(
                binding.manifest,
                artifact={
                    "verified": True,
                    "schema": artifact.metadata["schema"],
                    "repository": artifact.repository,
                    "commit": artifact.commit,
                    "views": list(artifact.views),
                },
            )
        else:
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
