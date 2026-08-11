# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Composed repository exploration for MCP clients.

``explore_context_impl`` turns CodeNib's existing ranked retrieval, static
LSP route, dependency graph, and verified source reader into one bounded
operation.  Narrow tools remain available, but callers no longer need a
model-tool-model round trip merely to turn graph locations into source.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...context_delivery import project_evidence_payloads
from ..explore_bounds import MAX_EXPLORE_PAYLOAD_BYTES, bound_explore_response
from ._validation import (
    MAX_EXPLORE_WINDOWS,
    MAX_SEARCH_QUERY_CHARS,
    MAX_SOURCE_WINDOW_LINES,
    bounded_integer,
    required_text,
)
from .dependency import dependency_subgraph_impl
from .lsp import lsp_route_impl, normalize_lsp_route_symbols
from .search import search_context_impl
from .source import read_source_impl


@dataclass(frozen=True, slots=True)
class _ExploreBudget:
    max_windows: int
    source_window_lines: int
    total_content_chars: int
    route_top_k: int
    dependency_symbols: int
    dependency_depth: int
    dependency_nodes: int
    dependency_edges: int


_BUDGETS = {
    "fast": _ExploreBudget(
        max_windows=4,
        source_window_lines=40,
        total_content_chars=8_000,
        route_top_k=8,
        dependency_symbols=1,
        dependency_depth=1,
        dependency_nodes=12,
        dependency_edges=32,
    ),
    "balanced": _ExploreBudget(
        max_windows=8,
        source_window_lines=80,
        total_content_chars=16_000,
        route_top_k=12,
        dependency_symbols=2,
        dependency_depth=2,
        dependency_nodes=24,
        dependency_edges=80,
    ),
    "thorough": _ExploreBudget(
        max_windows=12,
        source_window_lines=120,
        total_content_chars=32_000,
        route_top_k=20,
        dependency_symbols=3,
        dependency_depth=3,
        dependency_nodes=40,
        dependency_edges=160,
    ),
}
_DIRECTIONS = frozenset({"impact", "dependencies", "both"})
_MERGE_GAP_LINES = 8


@dataclass(slots=True)
class _Anchor:
    file: str
    symbol: str
    kind: str
    start_line: int | None
    end_line: int | None
    origins: list[str]
    rank: int
    score: float | None = None
    indexed_content: str | None = None


def _positive_line(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _anchor_from_payload(
    payload: Any,
    *,
    origin: str,
    rank: int,
) -> _Anchor | None:
    if not isinstance(payload, Mapping):
        return None
    file_path = payload.get("file")
    if not isinstance(file_path, str) or not file_path.strip():
        return None

    start_line = _positive_line(payload.get("start_line"))
    if start_line is None:
        start_line = _positive_line(payload.get("line"))
    end_line = _positive_line(payload.get("end_line"))
    if start_line is not None and (end_line is None or end_line < start_line):
        end_line = start_line

    symbol = payload.get("node_name") or payload.get("name") or payload.get("node_id")
    kind = payload.get("type") or payload.get("kind") or "symbol"
    score_value = payload.get("score")
    score = (
        float(score_value)
        if isinstance(score_value, (int, float)) and not isinstance(score_value, bool)
        else None
    )
    indexed_content = payload.get("content") if origin == "retrieval" else None
    return _Anchor(
        file=file_path.strip(),
        symbol=str(symbol or ""),
        kind=str(kind),
        start_line=start_line,
        end_line=end_line,
        origins=[origin],
        rank=rank,
        score=score,
        indexed_content=(indexed_content if isinstance(indexed_content, str) else None),
    )


def _ranges_overlap(left: _Anchor, right: _Anchor) -> bool:
    if left.start_line is None or right.start_line is None:
        return False
    left_end = left.end_line or left.start_line
    right_end = right.end_line or right.start_line
    return left.start_line <= right_end and right.start_line <= left_end


def _same_anchor(left: _Anchor, right: _Anchor) -> bool:
    if left.file != right.file:
        return False
    same_symbol = (
        bool(left.symbol) and left.symbol.casefold() == right.symbol.casefold()
    )
    same_start = (
        left.start_line is not None
        and right.start_line is not None
        and left.start_line == right.start_line
    )
    if same_symbol and (
        _ranges_overlap(left, right)
        or left.start_line is None
        or right.start_line is None
    ):
        return True
    return same_start and (same_symbol or left.kind == right.kind)


def _merge_anchor(existing: _Anchor, incoming: _Anchor) -> None:
    for origin in incoming.origins:
        if origin not in existing.origins:
            existing.origins.append(origin)
    if incoming.start_line is not None:
        existing.start_line = (
            incoming.start_line
            if existing.start_line is None
            else min(existing.start_line, incoming.start_line)
        )
    if incoming.end_line is not None:
        existing.end_line = (
            incoming.end_line
            if existing.end_line is None
            else max(existing.end_line, incoming.end_line)
        )
    if not existing.symbol:
        existing.symbol = incoming.symbol
    if existing.score is None or (
        incoming.score is not None and incoming.score > existing.score
    ):
        existing.score = incoming.score
    if existing.indexed_content is None:
        existing.indexed_content = incoming.indexed_content


def _collect_anchors(
    payloads: Sequence[Any],
    *,
    origin: str,
    anchors: list[_Anchor],
) -> None:
    for payload in payloads:
        incoming = _anchor_from_payload(
            payload,
            origin=origin,
            rank=len(anchors),
        )
        if incoming is None:
            continue
        existing = next(
            (candidate for candidate in anchors if _same_anchor(candidate, incoming)),
            None,
        )
        if existing is None:
            anchors.append(incoming)
        else:
            _merge_anchor(existing, incoming)


def _compact_anchor(anchor: _Anchor) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": anchor.symbol,
        "kind": anchor.kind,
        "origins": list(anchor.origins),
    }
    if anchor.start_line is not None:
        payload["start_line"] = anchor.start_line
    if anchor.end_line is not None:
        payload["end_line"] = anchor.end_line
    if anchor.score is not None:
        payload["score"] = anchor.score
    return payload


def _window_for_anchor(anchor: _Anchor, *, window_lines: int) -> tuple[int, int]:
    if anchor.start_line is None:
        return 1, window_lines
    start = anchor.start_line
    end = anchor.end_line or start
    span = end - start + 1
    if span >= window_lines:
        return start, start + window_lines - 1
    padding = window_lines - span
    first = max(1, start - padding // 2)
    return first, first + window_lines - 1


def _admit_windows(
    anchors: Sequence[_Anchor],
    *,
    max_windows: int,
    window_lines: int,
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for anchor in anchors:
        start_line, end_line = _window_for_anchor(
            anchor,
            window_lines=window_lines,
        )
        merged = False
        for window in windows:
            if window["file"] != anchor.file:
                continue
            if (
                start_line > int(window["end_line"]) + _MERGE_GAP_LINES
                or int(window["start_line"]) > end_line + _MERGE_GAP_LINES
            ):
                continue
            merged_start = min(start_line, int(window["start_line"]))
            merged_end = max(end_line, int(window["end_line"]))
            if merged_end - merged_start + 1 > MAX_SOURCE_WINDOW_LINES:
                continue
            window["start_line"] = merged_start
            window["end_line"] = merged_end
            window["anchors"].append(anchor)
            merged = True
            break
        if merged:
            continue
        if len(windows) >= max_windows:
            continue
        windows.append(
            {
                "file": anchor.file,
                "start_line": start_line,
                "end_line": end_line,
                "anchors": [anchor],
            }
        )
    return windows


def _content_fingerprint(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"sha256:{digest}"


def _indexed_context(window: Mapping[str, Any]) -> dict[str, Any]:
    anchors = list(window["anchors"])
    source_anchor = next(
        (anchor for anchor in anchors if anchor.indexed_content),
        None,
    )
    start_line = (
        source_anchor.start_line
        if source_anchor is not None and source_anchor.start_line is not None
        else int(window["start_line"])
    )
    end_line = (
        source_anchor.end_line
        if source_anchor is not None and source_anchor.end_line is not None
        else int(window["end_line"])
    )
    result: dict[str, Any] = {
        "file": window["file"],
        "start_line": start_line,
        "end_line": end_line,
        "verified": False,
        "content_kind": "indexed_excerpt" if source_anchor else "location_only",
        "anchors": [_compact_anchor(anchor) for anchor in anchors],
    }
    if source_anchor is not None and source_anchor.indexed_content is not None:
        result["content"] = source_anchor.indexed_content
        result["content_fingerprint"] = _content_fingerprint(
            source_anchor.indexed_content
        )
    return result


def _diagnostic(code: str, detail: Any) -> dict[str, str]:
    return {"code": code, "detail": str(detail)[:512]}


def _dependency_seed_candidates(
    explicit_symbols: Sequence[str],
    route_results: Sequence[Any],
    retrieval_results: Sequence[Any],
    *,
    limit: int,
) -> list[str]:
    candidates: list[str] = list(explicit_symbols)
    for payload in (*route_results, *retrieval_results):
        if not isinstance(payload, Mapping):
            continue
        value = payload.get("node_name") or payload.get("name")
        if isinstance(value, str):
            candidates.append(value)

    selected: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        selected.append(normalized)
        if len(selected) >= limit:
            break
    return selected


def _route_provider(ctx: Any, route_results: Sequence[Any]) -> dict[str, Any]:
    """Report the provider that produced route rows, including fallbacks."""

    for payload in route_results:
        if not isinstance(payload, Mapping):
            continue
        provider = payload.get("lsp_provider")
        if isinstance(provider, Mapping):
            return dict(provider)
    selection = getattr(ctx, "lsp_provider_selection", None)
    return dict(selection) if isinstance(selection, Mapping) else {}


def _source_identity(ctx: Any) -> dict[str, Any]:
    artifact = (
        ctx.artifact if isinstance(getattr(ctx, "artifact", None), Mapping) else {}
    )
    manifest = ctx.manifest
    payload: dict[str, Any] = {
        "repository": artifact.get("repository") or manifest.repo_path,
        "commit": manifest.commit,
        "source_fingerprint": manifest.source_fingerprint,
        "verified": getattr(ctx, "source_verified", False) is True,
    }
    if not payload["verified"]:
        payload["verification_error"] = (
            getattr(ctx, "source_error", None) or "source binding is unverified"
        )
    return payload


def explore_context_impl(
    ctx: Any,
    query: str,
    symbols: Sequence[str] | str = (),
    top_k: int = 8,
    budget: str = "balanced",
    direction: str = "both",
    include_dependencies: bool = True,
    filter_test: bool = False,
) -> dict[str, Any]:
    """Return ranked, routed, dependency-aware source context in one call."""

    normalized_query = required_text(
        query,
        name="query",
        maximum=MAX_SEARCH_QUERY_CHARS,
    )
    top_k = bounded_integer(top_k, name="top_k", maximum=MAX_EXPLORE_WINDOWS)
    normalized_budget = (budget or "").strip().lower()
    if normalized_budget not in _BUDGETS:
        raise ValueError("budget must be 'fast', 'balanced', or 'thorough'.")
    normalized_direction = (direction or "").strip().lower()
    if normalized_direction not in _DIRECTIONS:
        raise ValueError("direction must be 'impact', 'dependencies', or 'both'.")
    normalized_symbols = normalize_lsp_route_symbols(symbols)
    profile = _BUDGETS[normalized_budget]
    window_limit = min(top_k, profile.max_windows)

    diagnostics: list[dict[str, str]] = []
    retrieval_response: Mapping[str, Any] | None = None
    retrieval_results: list[Any] = []
    if (
        getattr(ctx, "bm25", None) is not None
        or getattr(ctx, "vector", None) is not None
    ):
        try:
            result = search_context_impl(
                ctx,
                normalized_query,
                top_k=max(window_limit, profile.route_top_k),
                budget=normalized_budget,
                level="l2",
                filter_test=bool(filter_test),
            )
            retrieval_response = result
            payloads = result.get("results", [])
            if isinstance(payloads, list):
                retrieval_results = payloads
        except (
            Exception
        ) as exc:  # noqa: BLE001 - composed backends degrade independently
            diagnostics.append(_diagnostic("retrieval_failed", exc))
    else:
        diagnostics.append(
            _diagnostic("retrieval_unavailable", "no ranked retrieval view is loaded")
        )

    route_results: list[Any] = []
    if (
        getattr(ctx, "lsp_provider", None) is not None
        or getattr(ctx, "symbol_graph", None) is not None
    ):
        try:
            routed = lsp_route_impl(
                ctx,
                symbols=normalized_symbols,
                query=normalized_query,
                top_k=profile.route_top_k,
                include_neighbors=True,
            )
            if isinstance(routed, list):
                route_results = routed
            elif isinstance(routed, Mapping) and routed.get("error"):
                diagnostics.append(_diagnostic("route_unavailable", routed["error"]))
        except (
            Exception
        ) as exc:  # noqa: BLE001 - composed backends degrade independently
            diagnostics.append(_diagnostic("route_failed", exc))
    else:
        diagnostics.append(
            _diagnostic("route_unavailable", "symbol_graph index not available")
        )

    anchors: list[_Anchor] = []
    _collect_anchors(retrieval_results, origin="retrieval", anchors=anchors)
    _collect_anchors(route_results, origin="route", anchors=anchors)

    relationships: list[dict[str, Any]] = []
    dependency_seeds: list[str] = []
    if include_dependencies and getattr(ctx, "symbol_graph", None) is not None:
        dependency_seeds = _dependency_seed_candidates(
            normalized_symbols,
            route_results,
            retrieval_results,
            limit=profile.dependency_symbols,
        )
        for seed in dependency_seeds:
            try:
                relationship = dependency_subgraph_impl(
                    ctx,
                    seed,
                    direction=normalized_direction,
                    depth=profile.dependency_depth,
                    max_nodes=profile.dependency_nodes,
                    max_edges=profile.dependency_edges,
                )
            except (
                Exception
            ) as exc:  # noqa: BLE001 - one seed must not lose all context
                diagnostics.append(_diagnostic("dependency_failed", f"{seed}: {exc}"))
                continue
            if relationship.get("error"):
                diagnostics.append(
                    _diagnostic("dependency_unavailable", relationship["error"])
                )
                continue
            relationships.append({"seed": seed, **relationship})
            nodes = relationship.get("nodes", [])
            if isinstance(nodes, list):
                _collect_anchors(nodes, origin="dependency", anchors=anchors)
    elif include_dependencies:
        diagnostics.append(
            _diagnostic(
                "dependency_unavailable",
                "symbol_graph index is not available for dependency expansion",
            )
        )

    windows = _admit_windows(
        anchors,
        max_windows=window_limit,
        window_lines=profile.source_window_lines,
    )
    source_identity = _source_identity(ctx)
    contexts: list[dict[str, Any]] = []
    for window in windows:
        if source_identity["verified"]:
            try:
                payload = read_source_impl(
                    ctx,
                    str(window["file"]),
                    int(window["start_line"]),
                    int(window["end_line"]),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                diagnostics.append(
                    _diagnostic("source_read_failed", f"{window['file']}: {exc}")
                )
                contexts.append(_indexed_context(window))
                continue
            content = str(payload.get("content") or "")
            context: dict[str, Any] = {
                "file": window["file"],
                "start_line": payload["start_line"],
                "end_line": payload["end_line"],
                "content": content,
                "content_fingerprint": _content_fingerprint(content),
                "content_kind": "live_source",
                "verified": True,
                "source": payload.get("source", source_identity),
                "anchors": [_compact_anchor(anchor) for anchor in window["anchors"]],
            }
            source_projection = payload.get("content_projection")
            if source_projection:
                context["source_projection"] = source_projection
            contexts.append(context)
        else:
            contexts.append(_indexed_context(window))

    projected_contexts = project_evidence_payloads(
        contexts,
        query=normalized_query,
        total_chars=profile.total_content_chars,
        min_excerpt_chars=600,
        max_excerpt_chars=4_800,
    )
    grouped_files: dict[str, dict[str, Any]] = {}
    for context in projected_contexts:
        file_path = str(context.pop("file"))
        group = grouped_files.setdefault(file_path, {"file": file_path, "contexts": []})
        group["contexts"].append(context)

    returned_content_chars = sum(
        len(str(context.get("content") or "")) for context in projected_contexts
    )
    retrieval_plan = (
        retrieval_response.get("plan") if retrieval_response is not None else None
    )
    response = {
        "query": normalized_query,
        "plan": {
            "name": "composed_explore_v1",
            "budget": normalized_budget,
            "retrieval": retrieval_plan,
            "route": {
                "symbols": normalized_symbols,
                "query_fallback": not normalized_symbols,
                "result_count": len(route_results),
                "provider": _route_provider(ctx, route_results),
            },
            "dependencies": {
                "enabled": bool(include_dependencies),
                "direction": normalized_direction,
                "seeds": dependency_seeds,
            },
            "delivery": {
                "max_windows": window_limit,
                "source_window_lines": profile.source_window_lines,
                "total_content_chars": profile.total_content_chars,
                "max_response_bytes": MAX_EXPLORE_PAYLOAD_BYTES,
                "response_measure": "mcp-call-result-json-utf8-v1",
            },
        },
        "source": source_identity,
        "summary": {
            "retrieval_hits": len(retrieval_results),
            "route_hits": len(route_results),
            "dependency_graphs": len(relationships),
            "anchors": len(anchors),
            "files": len(grouped_files),
            "source_windows": len(projected_contexts),
            "returned_content_chars": returned_content_chars,
        },
        "files": list(grouped_files.values()),
        "relationships": relationships,
        "diagnostics": diagnostics,
    }
    return bound_explore_response(response)


__all__ = ["explore_context_impl"]
