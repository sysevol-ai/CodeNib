# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Search MCP tools - vector (semantic), BM25, regex, and Zoekt wrappers
over backbone indexes."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from ...agent.boundary import to_agent_repr
from ...context_delivery import project_evidence_payloads
from ...types import NodeInfo
from ..context import ServerContext
from ._validation import (
    MAX_REGEX_FILTER_CHARS,
    MAX_SEARCH_QUERY_CHARS,
    MAX_TOOL_RESULTS,
    bounded_integer,
    bounded_text,
    required_text,
)


def _node_to_dict(node: NodeInfo) -> Dict[str, Any]:
    """Serialize a NodeInfo at the 1-based agent boundary."""
    return to_agent_repr(node)


def _project_nodes(nodes: List[NodeInfo], *, query: str) -> List[Dict[str, Any]]:
    return project_evidence_payloads(
        [_node_to_dict(node) for node in nodes],
        query=query,
    )


def search_context_impl(
    ctx: ServerContext,
    query: str,
    top_k: int = 10,
    budget: str = "balanced",
    level: str = "l2",
    filter_test: bool = False,
) -> Dict[str, Any]:
    """Plan and execute ranked retrieval over the available repository views."""
    normalized_query = required_text(
        query,
        name="query",
        maximum=MAX_SEARCH_QUERY_CHARS,
    )
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_TOOL_RESULTS)
    normalized_level = (level or "l2").strip().lower()
    if normalized_level not in {"l0", "l2"}:
        raise ValueError("level must be 'l0' or 'l2'.")

    from ...model.retrieval_planner import RetrievalCapabilities, RetrievalPlanner
    from ...ops.retrieve import execute_retrieval_stages

    capabilities = RetrievalCapabilities(
        has_dense=ctx.vector is not None,
        has_sparse=ctx.bm25 is not None,
        has_graph=ctx.symbol_graph is not None,
        has_embedding_rerank=False,
        has_llm_rerank=False,
    )
    planner = RetrievalPlanner()
    resolved_budget = planner.normalize_budget(budget)
    plan = planner.select(
        normalized_query,
        budget=resolved_budget,
        capabilities=capabilities,
    )

    def execute_stage(active_query: str, stage: Any) -> List[NodeInfo]:
        if stage.engine == "dense":
            if ctx.vector is None:
                raise RuntimeError("Dense retrieval selected without a vector index.")
            return ctx.vector.search_with_content(
                query=active_query,
                top_k=stage.top_k or plan.retrieval_top_k,
                level=normalized_level,
                score_threshold=None,
            )
        if stage.engine == "sparse":
            if ctx.bm25 is None:
                raise RuntimeError("Sparse retrieval selected without a BM25 index.")
            return ctx.bm25.search(
                query=active_query,
                top_k=stage.top_k or plan.retrieval_top_k,
                return_code_content=True,
                wrap_with_ln=False,
                filter_test=filter_test,
            )
        raise RuntimeError(f"Unsupported retrieval engine: {stage.engine!r}.")

    candidates = execute_retrieval_stages(
        normalized_query,
        plan.stages,
        execute_stage,
        top_k=plan.retrieval_top_k,
        fusion=plan.fusion,
    )
    if plan.graph is not None:
        from ...ops.expand import ExpandContext, expand_retrieval_candidates

        graph = plan.graph
        candidates = expand_retrieval_candidates(
            ExpandContext(code_graph=ctx.symbol_graph),
            candidates,
            seed_top_k=graph.seed_top_k,
            expand_top_k=graph.expand_top_k,
            hops=graph.hops,
            direction=graph.direction,
            use_ppr=graph.use_ppr,
            repo_path=ctx.manifest.repo_path,
            include_content=True,
        )

    graph_plan = None
    if plan.graph is not None:
        graph_plan = {
            "seed_top_k": plan.graph.seed_top_k,
            "expand_top_k": plan.graph.expand_top_k,
            "hops": plan.graph.hops,
            "direction": plan.graph.direction,
            "use_ppr": plan.graph.use_ppr,
        }
    artifact = ctx.artifact if isinstance(ctx.artifact, Mapping) else {}
    results = _project_nodes(candidates[:top_k], query=normalized_query)
    for result in results:
        score = result.get("score")
        if hasattr(score, "item"):
            result["score"] = float(score.item())
        elif isinstance(score, (int, float)):
            result["score"] = float(score)

    return {
        "plan": {
            "name": plan.name,
            "intent": plan.intent,
            "budget": resolved_budget.tier,
            "fusion": plan.fusion,
            "retrieval_top_k": plan.retrieval_top_k,
            "stages": [
                {
                    "engine": stage.engine,
                    "weight": stage.weight,
                    "top_k": stage.top_k,
                }
                for stage in plan.stages
            ],
            "graph": graph_plan,
            "rationale": plan.rationale,
        },
        "source": {
            "repository": artifact.get("repository") or ctx.manifest.repo_path,
            "commit": ctx.manifest.commit,
            "source_fingerprint": ctx.manifest.source_fingerprint,
        },
        "results": results,
    }


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
) -> list[dict] | dict[str, str]:
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
    normalized_query = required_text(
        query,
        name="query",
        maximum=MAX_SEARCH_QUERY_CHARS,
    )
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_TOOL_RESULTS)
    normalized_level = (level or "l2").strip().lower()
    if normalized_level not in {"l0", "l2"}:
        raise ValueError("level must be 'l0' or 'l2'.")
    if score_threshold is not None and (
        isinstance(score_threshold, bool)
        or not isinstance(score_threshold, (int, float))
        or not math.isfinite(float(score_threshold))
    ):
        raise ValueError("score_threshold must be a finite number.")
    if ctx.vector is None:
        return {
            "error": "Vector index not loaded. Re-run indexing with embedding enabled."
        }

    results = await asyncio.to_thread(
        ctx.vector.search_with_content,
        query=normalized_query,
        top_k=top_k,
        level=normalized_level,
        score_threshold=score_threshold,
    )

    result_dicts = _project_nodes(results, query=normalized_query)
    for node_dict in result_dicts:
        if hasattr(node_dict.get("score"), "item"):
            node_dict["score"] = float(node_dict["score"].item())
        elif isinstance(node_dict.get("score"), (int, float)):
            node_dict["score"] = float(node_dict["score"])
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
    normalized_query = required_text(
        query,
        name="query",
        maximum=MAX_SEARCH_QUERY_CHARS,
    )
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_TOOL_RESULTS)
    if ctx.bm25 is None:
        raise RuntimeError(
            "BM25 index is not available. "
            + ctx.errors.get("bm25", "No 'bm25' entry in manifest or status != fresh.")
        )

    results: List[NodeInfo] = ctx.bm25.search(
        query=normalized_query,
        top_k=top_k,
        return_code_content=True,
        wrap_with_ln=False,
        filter_test=filter_test,
    )
    return _project_nodes(results, query=normalized_query)


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
    required_text(
        pattern,
        name="pattern",
        maximum=MAX_SEARCH_QUERY_CHARS,
    )
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_TOOL_RESULTS)
    normalized_file_glob = (
        bounded_text(
            "" if file_glob is None else file_glob,
            name="file_glob",
            maximum=MAX_REGEX_FILTER_CHARS,
        )
        or None
    )
    normalized_node_type = (
        bounded_text(
            "" if node_type is None else node_type,
            name="node_type",
            maximum=MAX_REGEX_FILTER_CHARS,
        )
        or None
    )
    if ctx.regex_index is None:
        raise RuntimeError(
            "Regex index is not available. "
            + ctx.errors.get(
                "regex_index",
                "Requires a loaded symbol_graph (no 'symbol_graph' entry "
                "in manifest or load failed).",
            )
        )

    from ...index.regex_idx import RegexSearchBudgetError, RegexSearchTimeoutError

    try:
        results: List[NodeInfo] = ctx.regex_index.search(
            pattern=pattern,
            file_glob=normalized_file_glob,
            node_type=normalized_node_type,
            case_sensitive=case_sensitive,
            use_regex=True,
            top_k=top_k,
        )
    except (RegexSearchBudgetError, RegexSearchTimeoutError) as exc:
        raise RuntimeError(str(exc)) from exc
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid regex pattern {pattern!r}: {exc}. "
            "Use a shorter or valid regular expression."
        ) from exc

    return _project_nodes(results[:top_k], query=pattern)


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
        query: Zoekt query string. Plain substrings, regex (``regex:foo``),
            and atoms like ``case:yes`` / ``lang:python`` are all valid.
        top_k: Maximum number of file matches to return.
        file_filter: Optional glob/regex appended as ``file:<expr>``.

    Returns:
        List of dicts with keys: ``node_name`` (file path), ``type``
        (``"file"``), ``file``, ``start_line``, ``end_line``, ``content``,
        ``score``, ``node_id`` (language hint, when reported).
    """
    normalized_query = required_text(
        query,
        name="query",
        maximum=MAX_SEARCH_QUERY_CHARS,
    )
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_TOOL_RESULTS)
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
            query=normalized_query,
            top_k=top_k,
            file_filter=file_filter or None,
        )
    except ZoektUnavailableError as exc:
        raise RuntimeError(f"Zoekt search failed: {exc}") from exc

    return _project_nodes(results, query=normalized_query)
