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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Optional

from pydantic import Field

from ..compiler.manifest import RepoManifest
from ..log_utils import set_console_log_level
from .context import ServerContext
from .explore_session import ExploreSessionLedger, ExploreSessionRuntime
from .prompts import (
    CODENIB_EXPLORE_GUIDE,
    CODENIB_EXPLORE_INSTRUCTIONS,
    CODENIB_FULL_INSTRUCTIONS,
    CODENIB_GUIDE,
)
from .tool_surface import (
    TOOL_SURFACE_EXPLORE,
    TOOL_SURFACE_FULL,
    TOOL_SURFACES,
    ToolSurfaceMCPServer,
)
from .tools._validation import (
    MAX_DEPENDENCY_EDGES,
    MAX_EXPLORE_WINDOWS,
    MAX_GRAPH_DEPTH,
    MAX_LSP_POSITION,
    MAX_LSP_QUERY_CHARS,
    MAX_LSP_SYMBOL_CHARS,
    MAX_REGEX_FILTER_CHARS,
    MAX_REGEX_PATTERN_CHARS,
    MAX_ROUTE_SYMBOLS,
    MAX_SEARCH_QUERY_CHARS,
    MAX_SOURCE_PATH_CHARS,
    MAX_TOOL_RESULTS,
)
from .tools.dependency import dependency_subgraph_impl
from .tools.explore import explore_context_impl
from .tools.lsp import lsp_definition_impl, lsp_references_impl, lsp_route_impl
from .tools.search import search_bm25_impl, search_context_impl, search_regex_impl
from .tools.search import search_semantic as _search_semantic_impl
from .tools.search import search_zoekt_impl
from .tools.source import read_source_impl

if TYPE_CHECKING:
    from ..artifacts.runtime import ContextArtifactBinding
    from ..source_fingerprint import RepositorySourceBinding

logger = logging.getLogger(__name__)

# Historical embedders import ``MCPServer`` from this module. Keep that alias
# while specializing the server with opt-in tool visibility.
MCPServer = ToolSurfaceMCPServer

# Global context is set once at startup before the event loop runs tools.
_ctx: Optional[ServerContext] = None
_SearchText = Annotated[str, Field(min_length=1)]
_SearchQuery = Annotated[
    str,
    Field(min_length=1, max_length=MAX_SEARCH_QUERY_CHARS),
]
_SearchTopK = Annotated[int, Field(ge=1, le=MAX_TOOL_RESULTS)]
_RegexPattern = Annotated[
    str,
    Field(min_length=1, max_length=MAX_REGEX_PATTERN_CHARS),
]
_RegexFilter = Annotated[str, Field(max_length=MAX_REGEX_FILTER_CHARS)]
_SearchLevel = Literal["l0", "l2"]
_FiniteScore = Annotated[float, Field(allow_inf_nan=False)]
_GraphDirection = Literal["impact", "dependencies", "both"]
_GraphDepth = Annotated[int, Field(ge=1, le=MAX_GRAPH_DEPTH)]
_DependencyEdges = Annotated[int, Field(ge=1, le=MAX_DEPENDENCY_EDGES)]
_PositiveLine = Annotated[int, Field(ge=1, le=MAX_LSP_POSITION)]
_Character = Annotated[int, Field(ge=0, le=MAX_LSP_POSITION)]
_LspFilePath = Annotated[str, Field(max_length=MAX_SOURCE_PATH_CHARS)]
_LspSymbol = Annotated[str, Field(max_length=MAX_LSP_SYMBOL_CHARS)]
_LspQuery = Annotated[str, Field(max_length=MAX_LSP_QUERY_CHARS)]
_RouteSymbols = Annotated[
    list[_LspSymbol],
    Field(max_length=MAX_ROUTE_SYMBOLS),
]
_SourcePath = Annotated[str, Field(min_length=1, max_length=MAX_SOURCE_PATH_CHARS)]
_ExploreTopK = Annotated[int, Field(ge=1, le=MAX_EXPLORE_WINDOWS)]
_ExploreBudget = Literal["fast", "balanced", "thorough"]


@asynccontextmanager
async def _mcp_lifespan(
    _server: Any,
) -> AsyncIterator[ExploreSessionRuntime | None]:
    """Own one fresh exploration runtime for each transport connection."""

    ctx = _ctx
    runtime = ctx.begin_explore_session() if ctx is not None else None
    try:
        yield runtime
    finally:
        if ctx is not None and runtime is not None:
            ctx.end_explore_session(runtime)


def get_context() -> ServerContext:
    """Return the loaded ServerContext or raise if uninitialized."""
    if _ctx is None:
        raise RuntimeError("ServerContext not initialized. Call init_server() first.")
    return _ctx


# MCPServer configures the process-wide root logger while it is constructed.
# Keep imports safe for embedding applications; `main()` configures logging
# explicitly after parsing the requested CLI level.
_root_logger = logging.getLogger()
_import_logging_guard: logging.NullHandler | None = None
if not _root_logger.handlers:
    _import_logging_guard = logging.NullHandler()
    _root_logger.addHandler(_import_logging_guard)
try:
    mcp = MCPServer(
        "codenib",
        instructions=CODENIB_FULL_INSTRUCTIONS,
        lifespan=_mcp_lifespan,
    )
finally:
    if _import_logging_guard is not None:
        _root_logger.removeHandler(_import_logging_guard)


def configure_tool_surface(value: str) -> str:
    """Apply one surface consistently to discovery, calls, and instructions."""

    surface = mcp.set_tool_surface(value)
    mcp._lowlevel_server.instructions = (  # noqa: SLF001 - SDK has no setter
        CODENIB_EXPLORE_INSTRUCTIONS
        if surface == TOOL_SURFACE_EXPLORE
        else CODENIB_FULL_INSTRUCTIONS
    )
    return surface


def _finish_abandoned_explore_worker(
    runtime: ExploreSessionRuntime,
    worker: asyncio.Task[Any],
) -> None:
    """Consume a cancelled caller's worker and release its runtime barrier."""

    if runtime.pending_worker is worker:
        runtime.pending_worker = None
    if not worker.cancelled():
        worker.exception()


async def _wait_for_abandoned_explore_worker(
    runtime: ExploreSessionRuntime,
) -> None:
    """Keep backend access serialized after an earlier caller cancelled."""

    worker = runtime.pending_worker
    if worker is None:
        return
    try:
        await asyncio.shield(worker)
    except Exception:  # noqa: BLE001 - the abandoned call was already cancelled
        pass
    finally:
        if worker.done() and runtime.pending_worker is worker:
            runtime.pending_worker = None


# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------


@mcp.tool(
    name="explore_context",
    description=(
        "Recommended bounded repository exploration. Composes ranked retrieval, "
        "the selected LSP-shaped route, dependency expansion, and verified source "
        "windows. Returns the concrete provider plan, source identity, independent "
        "diagnostics, and per-connection delivery usage."
    ),
)
async def explore_context(
    query: _SearchQuery,
    symbols: _RouteSymbols | None = None,
    top_k: _ExploreTopK = 8,
    budget: _ExploreBudget = "balanced",
    direction: _GraphDirection = "both",
    include_dependencies: bool = True,
    filter_test: bool = False,
) -> dict[str, Any]:
    """Compose repository context and commit delivery history atomically."""

    ctx = get_context()
    runtime = ctx.ensure_explore_session()
    async with runtime.call_lock:
        await _wait_for_abandoned_explore_worker(runtime)
        worker = asyncio.create_task(
            asyncio.to_thread(
                explore_context_impl,
                ctx,
                query,
                symbols or (),
                top_k,
                budget,
                direction,
                include_dependencies,
                filter_test,
            )
        )
        try:
            response = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # ``to_thread`` cannot stop an already-running function. Let it
            # finish read-only and never commit it. Cancellation remains
            # responsive; the next call waits on this worker before touching
            # the same providers.
            runtime.pending_worker = worker
            worker.add_done_callback(
                lambda completed: _finish_abandoned_explore_worker(
                    runtime,
                    completed,
                )
            )
            raise
        return runtime.ledger.project(response)


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
    query: _SearchQuery,
    top_k: _SearchTopK = 10,
    budget: str = "balanced",
    level: _SearchLevel = "l2",
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
    query: _SearchQuery,
    top_k: _SearchTopK = 10,
    level: _SearchLevel = "l2",
    score_threshold: _FiniteScore = 0.0,
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
    query: _SearchQuery,
    top_k: _SearchTopK = 20,
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
    pattern: _RegexPattern,
    top_k: _SearchTopK = 20,
    file_glob: _RegexFilter = "",
    node_type: _RegexFilter = "",
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
    query: _SearchQuery,
    top_k: _SearchTopK = 20,
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
        "visualization. max_nodes includes the queried root; max_edges bounds "
        "relationships independently. Symbols are fuzzy-matched; unresolved "
        "names return a note."
    ),
)
async def dependency_subgraph(
    symbol: _SearchText,
    direction: _GraphDirection = "both",
    depth: _GraphDepth = 2,
    max_nodes: _SearchTopK = 60,
    max_edges: _DependencyEdges = 400,
) -> dict[str, Any]:
    """Call-graph subgraph for *symbol*, bounded by node and edge budgets."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return await asyncio.to_thread(
        dependency_subgraph_impl,
        _ctx,
        symbol,
        direction,
        depth,
        max_nodes,
        max_edges,
    )


@mcp.tool(
    name="lsp_definition",
    description=(
        "Return compact definition locations from CodeNib's runtime LSP "
        "provider or persisted symbol graph fallback. Provide either symbol "
        "or file_path + 1-based line. Results are locations only; call "
        "read_source before finalizing."
    ),
)
async def lsp_definition(
    file_path: _LspFilePath = "",
    line: _PositiveLine | None = None,
    character: _Character | None = None,
    symbol: _LspSymbol = "",
    top_k: _SearchTopK = 8,
) -> list[dict[str, Any]] | dict[str, str]:
    """Provider-backed definition lookup with persisted-graph fallback."""
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
        "Return compact definition/reference locations from CodeNib's runtime "
        "LSP provider or persisted symbol graph fallback. Provide either "
        "symbol or file_path + 1-based line. Results are locations only; call "
        "read_source before finalizing."
    ),
)
async def lsp_references(
    file_path: _LspFilePath = "",
    line: _PositiveLine | None = None,
    character: _Character | None = None,
    symbol: _LspSymbol = "",
    include_declaration: bool = True,
    top_k: _SearchTopK = 40,
) -> list[dict[str, Any]] | dict[str, str]:
    """Provider-backed reference lookup with persisted-graph fallback."""
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
        "Return compact route anchors from CodeNib's runtime LSP provider or "
        "persisted symbol graph fallback for symbol seeds, or use a query "
        "alone when no reliable symbol is known. Use this for a route map "
        "across endpoint, bridge/factory, provider/value, or type anchors. "
        "Results are locations only; call read_source before finalizing."
    ),
)
async def lsp_route(
    symbols: _RouteSymbols,
    query: _LspQuery = "",
    top_k: _SearchTopK = 12,
    include_neighbors: bool = True,
) -> list[dict[str, Any]] | dict[str, str]:
    """Provider-backed route map with persisted-graph fallback."""
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
    name="read_source",
    description=(
        "Read up to 200 lines authenticated by the retained source-content "
        "binding using a "
        "repository-relative POSIX path and 1-based inclusive line range. "
        "Responses are bounded; commit is display provenance, not attested."
    ),
)
async def read_source(
    file_path: _SourcePath,
    start_line: _PositiveLine = 1,
    end_line: _PositiveLine | None = None,
) -> dict[str, Any]:
    """Return one bounded window from content-authenticated source bytes."""
    if _ctx is None:
        raise RuntimeError("Server not initialized")
    return await asyncio.to_thread(
        read_source_impl,
        _ctx,
        file_path,
        start_line,
        end_line,
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
    source_verified = _ctx.verify_source_status()
    result = _ctx.manifest.to_dict()
    explore_runtime = getattr(_ctx, "explore_runtime", None)
    explore_session = (
        explore_runtime.ledger.stats()
        if isinstance(explore_runtime, ExploreSessionRuntime)
        else ExploreSessionLedger().stats()
    )
    result["runtime"] = {
        "loaded_views": sorted(_ctx.loaded_views),
        "view_errors": dict(sorted(_ctx.errors.items())),
        "tool_surface": mcp.tool_surface,
        "explore_session": explore_session,
        "source_read": {
            "verified": source_verified,
            "error": _ctx.source_error,
            "verification_scope": _ctx.source_verification_scope,
            "commit_verified": False,
            "checkout_state": "not-attested",
        },
        "lsp_provider": dict(_ctx.lsp_provider_selection),
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
    if mcp.tool_surface == TOOL_SURFACE_EXPLORE:
        return CODENIB_EXPLORE_GUIDE
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

        lsp_selection = ctx.lsp_provider_selection
        lines.append(
            "  lsp_provider: "
            f"{lsp_selection.get('backend', 'unavailable')} "
            f"({lsp_selection.get('status', 'unavailable')})"
        )

        if ctx.zoekt is not None:
            lines.append(f"  ✓ zoekt: port={ctx.zoekt.port}")
        else:
            lines.append("  ✗ zoekt: not_loaded")

        source_verified = ctx.verify_source_status()
        source_status = (
            "verified(content-bytes; commit=not-attested)"
            if source_verified
            else (ctx.source_error or "unverified")
        )
        lines.append(f"  source_read: {source_status}")

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
    parser.add_argument(
        "--tool-surface",
        choices=sorted(TOOL_SURFACES),
        default=TOOL_SURFACE_FULL,
        help="MCP tool discovery and call surface.",
    )
    return parser.parse_args(argv)


def init_server(
    manifest_path: RepoManifest | str | Path,
    *,
    artifact: dict[str, Any] | None = None,
    artifact_binding: ContextArtifactBinding | None = None,
    source_binding: RepositorySourceBinding | None = None,
) -> None:
    """Initialize the global ServerContext from a manifest file.

    Loads the manifest and opens all available indexes in the
    module-level ``_ctx``. Safe to call from tests with a temporary
    manifest path.

    Raises:
        FileNotFoundError: if ``manifest_path`` does not exist.
    """
    global _ctx
    resolved_manifest_path: Path | None = None
    if isinstance(manifest_path, RepoManifest):
        manifest = manifest_path
        logger.info(
            "Loading in-memory manifest for %s@%s",
            manifest.repo_path,
            (manifest.commit or "")[:12],
        )
        compiler_lock = nullcontext()
    else:
        resolved = Path(manifest_path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Manifest not found: {resolved}")
        logger.info("Loading manifest from %s", resolved)
        resolved_manifest_path = resolved
        # A local manifest is an explicit administrator boundary. Serialize
        # capture and native loading with the same cache lock as IndexCompiler.
        # Artifact bindings intentionally skip this branch and remain inert.
        from filelock import FileLock

        compiler_lock = FileLock(str(resolved.parent / ".index-compiler.lock"))

    from ..artifacts.runtime import (
        SourceBindingCleanupOwner,
        _raise_source_cleanup_failure,
        _source_cleanup_owner_is_pending,
    )

    capture_cleanup_owner: SourceBindingCleanupOwner | None = None
    new_context: ServerContext | None = None

    def close_capture_owner(
        primary: BaseException,
    ) -> tuple[BaseException | None, object | None]:
        """Close the preinstalled owner and identify any retry authority."""

        cleanup_failure: BaseException | None = None
        if capture_cleanup_owner is not None:
            try:
                capture_cleanup_owner.close()
            except BaseException as exc:  # noqa: B036 - caller selects priority
                cleanup_failure = exc
        pending_owner: object | None = None
        if _source_cleanup_owner_is_pending(capture_cleanup_owner):
            pending_owner = capture_cleanup_owner
        nested_owner = getattr(primary, "source_cleanup_owner", None)
        if pending_owner is None and _source_cleanup_owner_is_pending(nested_owner):
            pending_owner = nested_owner
        return cleanup_failure, pending_owner

    try:
        with compiler_lock:
            if resolved_manifest_path is not None:
                manifest = RepoManifest.load(resolved_manifest_path)

            from ..compiler.manifest_source import (
                capture_repository_source_for_manifest,
                require_manifest_source_identity,
            )
            from ..source_fingerprint import is_secure_source_fingerprint_v2

            source_error: str | None = "source binding has not been verified"
            retained_source = (
                artifact_binding.source_binding
                if artifact_binding is not None
                else source_binding
            )
            verified = False
            if retained_source is not None and is_secure_source_fingerprint_v2(
                manifest.source_fingerprint
            ):
                retained_identity = retained_source.authenticated_identity_snapshot()
                require_manifest_source_identity(
                    retained_identity,
                    manifest,
                    label="MCP repository",
                    mismatch_message=(
                        "repository source bytes do not match the indexed content"
                    ),
                )
                verified = True
            native_authorization = None
            if verified:
                source_error = None
            elif resolved_manifest_path is not None:
                from ..source_fingerprint import lexical_repository_path

                repo_path = lexical_repository_path(manifest.repo_path)
                if not is_secure_source_fingerprint_v2(manifest.source_fingerprint):
                    source_error = (
                        "manifest has no trust-eligible source fingerprint v2"
                    )
                else:
                    capture_cleanup_owner = SourceBindingCleanupOwner()
                    try:
                        retained_source = capture_repository_source_for_manifest(
                            repo_path,
                            manifest,
                            exclude_roots=(resolved_manifest_path.parent,),
                            _source_owner=capture_cleanup_owner.retain,
                        )
                        retained_identity = (
                            retained_source.authenticated_identity_snapshot()
                        )
                        require_manifest_source_identity(
                            retained_identity,
                            manifest,
                            label="MCP repository",
                            mismatch_message=(
                                "repository source bytes do not match the indexed content"
                            ),
                        )
                    except BaseException as exc:  # noqa: B036 - preserve primary
                        cleanup_failure, pending_owner = close_capture_owner(exc)
                        retained_source = None
                        if (
                            not isinstance(exc, Exception)
                            or cleanup_failure is not None
                            or pending_owner is not None
                        ):
                            _raise_source_cleanup_failure(
                                exc,
                                cleanup_failure,
                                pending_owner,
                            )
                        source_error = str(exc)
                    else:
                        verified = True
                        source_error = None

                if verified and manifest.index_is_current("vector"):
                    from ..index.embedding.artifact_integrity import (
                        capture_authenticated_vector_view,
                    )
                    from ..native_index_authorization import (
                        _mint_trusted_local_admin_authorization,
                    )

                    entry = manifest.indexes["vector"]
                    with capture_authenticated_vector_view(entry.path) as vector_view:
                        native_authorization = _mint_trusted_local_admin_authorization(
                            vector_view.ownership,
                            view_type="vector",
                            semantic_contract=entry.config,
                            evidence=(
                                "local-admin-cli-intent",
                                "source-content-fingerprint-v2-bound",
                                "captured-vector-tree-subject",
                            ),
                        )

            new_context = ServerContext.load(
                manifest,
                artifact=artifact,
                artifact_binding=artifact_binding,
                native_index_authorization=native_authorization,
                source_binding=retained_source,
            )
            new_context.source_error = source_error
            portable_artifact = artifact is not None or artifact_binding is not None
            new_context.configure_lsp_provider(
                allow_native=new_context.source_verified and not portable_artifact,
                native_disabled_reason=(
                    "portable_artifact_uses_persisted_graph"
                    if portable_artifact
                    else "local_source_not_verified"
                ),
            )

        if _ctx is not None:
            _ctx.close()
        _ctx = new_context
        if not _ctx.source_verified:
            logger.warning("Source reads disabled: %s", _ctx.source_error)
    except BaseException as primary:  # noqa: B036 - retain capture owner
        # Once the new context is globally reachable it is the stable owner.
        # Before that point, the preinstalled capture owner must close or retain
        # the source across any return-handoff or startup cancellation.
        if not (new_context is not None and _ctx is new_context):
            cleanup_failure: BaseException | None = None
            pending_owner: object | None = None
            context_source = (
                new_context._source_binding if new_context is not None else None
            )
            if new_context is not None:
                try:
                    new_context.close()
                except BaseException as exc:  # noqa: B036 - apply shared priority
                    cleanup_failure = exc
            if _source_cleanup_owner_is_pending(context_source):
                pending_owner = context_source
            if capture_cleanup_owner is not None:
                owner_failure, owner_pending = close_capture_owner(primary)
                if cleanup_failure is None:
                    cleanup_failure = owner_failure
                if pending_owner is None:
                    pending_owner = owner_pending
            if cleanup_failure is not None or pending_owner is not None:
                _raise_source_cleanup_failure(
                    primary,
                    cleanup_failure,
                    pending_owner,
                )
        raise


def serve_retained_context(
    owner: object,
    *,
    log_level: str = "INFO",
    tool_surface: str = TOOL_SURFACE_FULL,
) -> None:
    """Serve one preloaded retained context for a single stdio lifetime.

    This is deliberately a cold-start boundary.  It never replaces an
    existing module context and it detaches the exact context before closing
    the caller-created owner.  Ref polling and live replacement require the
    request-pinned lifecycle planned for M3.
    """

    from .._atomic_directory import (
        _OrderedAction,
        _run_context_with_cleanup_actions,
    )
    from .retained_context import RetainedServerContextOwner

    if type(owner) is not RetainedServerContextOwner:
        raise TypeError("retained MCP owner must use the exact owner type")

    global _ctx
    context: ServerContext | None = None

    def detached() -> bool:
        if context is None:
            return True
        return _ctx is None

    def detach() -> None:
        global _ctx
        if context is None or _ctx is None:
            return
        if _ctx is not context:
            raise RuntimeError("MCP ServerContext changed during retained serving")
        _ctx = None

    cleanup_actions = (
        _OrderedAction(
            label="retained MCP context detachment also failed",
            action=detach,
            complete=detached,
            retry_incomplete="cancellation",
            incomplete_owner=owner,
        ),
        _OrderedAction(
            label="retained MCP context cleanup also failed",
            action=owner.close,
            complete=lambda: owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=owner,
        ),
    )
    with _run_context_with_cleanup_actions(cleanup_actions):
        set_console_log_level(log_level)
        configure_tool_surface(tool_surface)
        context = owner.context
        if _ctx is not None:
            raise RuntimeError("ServerContext is already initialized")
        _ctx = context
        logger.info("Starting retained MCP server on stdio...")
        mcp.run(transport="stdio")


def main(argv: list[str] | None = None) -> None:
    """Run the ``codenib-mcp`` console entry point."""
    program_name = _cli_program_name()
    args = _parse_args(argv)
    set_console_log_level(args.log_level)
    configure_tool_surface(args.tool_surface)
    manifest_path = args.manifest_flag or args.manifest
    if args.artifact and manifest_path:
        logger.error("Choose either a manifest or --artifact, not both")
        sys.exit(1)
    if not args.artifact and not manifest_path:
        logger.error(
            "No context provided. Use: %s <manifest> or --artifact <dir> "
            "[--repo <dir>]",
            program_name,
        )
        sys.exit(1)

    try:
        if args.artifact:
            if args.repo:
                from ..artifacts import bind_context_artifact

                binding = bind_context_artifact(
                    args.artifact,
                    args.repo,
                    expected_repository=args.repository,
                )
            else:
                from ..artifacts import query_context_artifact

                binding = query_context_artifact(
                    args.artifact,
                    expected_repository=args.repository,
                )
            artifact = binding.artifact
            try:
                init_server(
                    binding.manifest,
                    artifact={
                        "verified": True,
                        "schema": artifact.metadata["schema"],
                        "repository": artifact.repository,
                        "commit": artifact.commit,
                        "views": list(artifact.views),
                    },
                    artifact_binding=binding,
                )
            except BaseException:  # noqa: B036 - preserve startup failure
                try:
                    binding.close()
                except BaseException:  # noqa: B036 - preserve primary failure
                    pass
                raise
            binding.close()
        else:
            init_server(manifest_path)
        logger.info("Starting MCP server on stdio...")
        mcp.run(transport="stdio")
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.error("Failed to start server: %s", exc)
        logger.debug("MCP server startup failure", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
