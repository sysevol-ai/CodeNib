# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Executor factory for the ``graph_expand`` skill.

LSP-aligned 1-hop range / symbol query. Given one or more source ranges
(file + line span) or named symbols, returns the symbols adjacent to each
seed, tagged by ``role`` (defined / callees / callers), built directly on
``CodeGraph.query_range`` / ``CodeGraph.query_range_by_symbol``.

Line-numbering convention (CLAUDE.md): the ``ranges`` input is **1-based,
inclusive** (agent-facing), matching ``CodeLocation`` and BM25/embedding
result line numbers. ``CodeGraph.query_range`` expects **0-based** spans, so
each range is converted ``-1`` locally before the call. Symmetrically, the
``anchor_line`` carried by ``query_range`` edges is 0-based; it is converted
``+1`` back to 1-based for output. The dedicated agent-boundary helper from
#153 lands on a separate branch, so the conversion is done inline here.

Each result carries ``role``, ``edge_kind``, and the call-site ``anchor_*``
so the LLM can reason about graph relationships without reading code. Use a
file-reading tool to fetch any body the LLM decides is worth inspecting.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


def _normalize_path(p: Optional[str]) -> Optional[str]:
    """Match ``retrieval_eval.normalize_file_path`` so file keys agree."""
    if not p:
        return None
    return str(Path(p).as_posix()).lstrip("./")


def create_executor(context: Any) -> Callable[..., List[Any]]:
    """Factory: return a callable that runs ``graph_expand`` against ``context``.

    Parameters
    ----------
    context:
        An ``ExpandContext`` (from ``codeminer.ops.expand``) carrying the
        loaded ``CodeGraph``.
    """
    from ....graph.code_graph import CodeGraph
    from ....types import EDGE_TYPE_REFERENCE, QueriedNode
    from ....utils import is_test_file

    _VALID_MODES = {"defined", "callees", "callers", "all"}
    _ROLE_ORDER: Dict[str, int] = {"defined": 0, "callees": 1, "callers": 2}

    def _build_node_id(file: Optional[str], display: str) -> str:
        f = file or ""
        return f"{f}:{display}" if f else display

    def _to_one_based_line(line: Optional[int]) -> Optional[int]:
        """0-based graph line -> 1-based agent-facing line."""
        return None if line is None else line + 1

    # graph_expand has no relevance signal — `score` is left at 0.0 (matches
    # bm25_search's convention when scores aren't available). Result ordering
    # is driven by `role` via `_ROLE_ORDER` below, not by score.

    def _node_ref_to_queried(ref: Any, role: str) -> QueriedNode:
        display = ref.unified_name or ref.name
        return QueriedNode(
            node_name=display,
            type=ref.kind,
            file=ref.file,
            node_id=_build_node_id(ref.file, display),
            start_line=_to_one_based_line(ref.start_line),
            end_line=_to_one_based_line(ref.end_line),
            score=0.0,
            role=role,
            # `defined` carries no edge — anchor_* and edge_kind stay None.
        )

    def _vid_to_queried_with_edge(
        graph: CodeGraph, vid: int, edge: Any, role: str
    ) -> Optional[QueriedNode]:
        attrs = graph.graph.vs[vid].attributes()
        name = graph.graph.vs[vid]["name"]
        file = attrs.get("file")
        if file is None:
            return None
        display = attrs.get("unified_name") or name
        return QueriedNode(
            node_name=display,
            type=attrs.get("type", ""),
            file=file,
            node_id=_build_node_id(file, display),
            start_line=_to_one_based_line(attrs.get("start_line")),
            end_line=_to_one_based_line(attrs.get("end_line")),
            score=0.0,
            role=role,
            edge_kind=edge.edge_kind,
            anchor_file=edge.anchor_file,
            anchor_line=_to_one_based_line(edge.anchor_line),
        )

    def _flatten_result(graph: CodeGraph, result: Any, mode: str) -> List[QueriedNode]:
        """Convert a ``RangeQueryResult`` into role-tagged ``QueriedNode``s."""
        out: List[QueriedNode] = []

        if mode in ("defined", "all"):
            for n in result.defined:
                out.append(_node_ref_to_queried(n, role="defined"))

        if mode in ("callees", "all"):
            seen_targets: Set[int] = set()
            for e in result.outgoing:
                if e.edge_kind != EDGE_TYPE_REFERENCE:
                    continue
                if e.target_vid in seen_targets:
                    continue
                seen_targets.add(e.target_vid)
                node = _vid_to_queried_with_edge(graph, e.target_vid, e, role="callees")
                if node is not None:
                    out.append(node)

        if mode in ("callers", "all"):
            seen_sources: Set[int] = set()
            for e in result.incoming:
                if e.edge_kind != EDGE_TYPE_REFERENCE:
                    continue
                if e.source_vid in seen_sources:
                    continue
                seen_sources.add(e.source_vid)
                node = _vid_to_queried_with_edge(graph, e.source_vid, e, role="callers")
                if node is not None:
                    out.append(node)

        return out

    def _resolve_symbol_to_identities(graph: CodeGraph, symbol: str) -> List[str]:
        """Resolve an LLM-supplied symbol name to identity names.

        Tries identity exact match first; falls back to ``_unified_to_names``
        reverse lookup (display ``unified_name`` -> [identities]). Returns the
        list of identity names (possibly empty for unknown symbols).
        """
        if symbol in graph.name_to_vertex:
            return [symbol]
        return list(graph._unified_to_names.get(symbol, []))

    @lru_cache(maxsize=1024)
    def _resolve_cached(graph_key: int, symbol: str) -> Tuple[str, ...]:
        return tuple(_resolve_symbol_to_identities(context.code_graph, symbol))

    def _truncate_with_hint(
        results: List[QueriedNode], top_k: int
    ) -> List[QueriedNode]:
        """Apply top_k cap and append a sentinel record describing truncation."""
        if len(results) <= top_k:
            return results
        kept = results[:top_k]
        sentinel = QueriedNode(
            node_name="__truncated__",
            type="meta",
            file=None,
            node_id=None,
            start_line=None,
            end_line=None,
            score=0.0,
            content=(
                f"truncated: returned top_k={top_k} of {len(results)} total; "
                f"narrow the ranges or set mode= to a single role to see more"
            ),
        )
        kept.append(sentinel)
        return kept

    def _parse_ranges(raw: Any) -> List[Tuple[str, int, int]]:
        """Normalize the ``ranges`` input into (file, start0, end0) tuples.

        Input lines are 1-based inclusive (agent-facing); they are converted to
        0-based here before being handed to ``query_range``. Malformed entries
        are skipped silently (matches LSP tolerance for partial client state).
        """
        if not raw:
            return []
        out: List[Tuple[str, int, int]] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            file = _normalize_path(r.get("file"))
            s = r.get("start_line")
            e = r.get("end_line")
            if file is None or s is None or e is None:
                continue
            try:
                si, ei = int(s), int(e)
            except (TypeError, ValueError):
                # LLM-generated tool-call JSON may carry non-numeric strings
                # in numeric fields ("abc", "12.5", lists). Drop, don't crash.
                continue
            if isinstance(s, bool) or isinstance(e, bool):
                # bool is an int subclass; a JSON `true` is never a line number.
                continue
            if si > ei:
                si, ei = ei, si
            # 1-based inclusive -> 0-based inclusive. Clamp at 0 so a stray
            # 0/negative agent line never underflows into a bogus query.
            si0 = max(si - 1, 0)
            ei0 = max(ei - 1, 0)
            out.append((file, si0, ei0))
        return out

    def execute(
        ranges: Optional[List[Dict[str, Any]]] = None,
        symbols: Optional[List[str]] = None,
        mode: str = "all",
        top_k: int = 50,
        filter_tests: bool = True,
        hops: int = 1,
        **kwargs: Any,
    ) -> List[QueriedNode]:
        graph = context.code_graph
        if graph is None:
            raise RuntimeError("Symbol graph not available")

        if mode not in _VALID_MODES:
            raise ValueError(
                f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}"
            )

        # `hops` is reserved in the API for forward compatibility (see module
        # docstring) but only depth=1 is implemented today, because
        # CodeGraph.query_range itself only supports depth=1. Any other value
        # is silently treated as 1 — no clamp/assignment needed since `hops`
        # is not read again below.
        del hops

        if not graph._file_nodes and not graph._unified_to_names:
            raise RuntimeError(
                "Range indexes empty — call CodeGraph.build_range_indexes() "
                "before querying. Likely a partially-built or pre-v3 graph."
            )

        # Distinguish "no input shape was provided at all" (a usage error worth
        # raising on) from "an input list was given but every entry normalized
        # away" (degenerate but valid — return []). Empty-string / whitespace /
        # non-string symbols and malformed ranges fall into the latter bucket.
        if not ranges and not symbols:
            raise ValueError(
                "graph_expand needs either `ranges` "
                "([{file, start_line, end_line}, ...], 1-based lines) or "
                "`symbols` ([identity_or_unified_name, ...])"
            )

        norm_ranges = _parse_ranges(ranges)
        norm_symbols = [s for s in (symbols or []) if isinstance(s, str) and s.strip()]

        results: List[QueriedNode] = []

        for file, s, e in norm_ranges:
            r = graph.query_range(file, s, e)
            results.extend(_flatten_result(graph, r, mode))

        for sym in norm_symbols:
            identities = _resolve_cached(id(graph), sym)
            for identity in identities:
                r = graph.query_range_by_symbol(identity)
                results.extend(_flatten_result(graph, r, mode))

        if filter_tests:
            results = [
                n for n in results if not (n.node_id and is_test_file(n.node_id))
            ]

        # Dedup across multiple seeds + multiple overloads sharing the same
        # unified_name in the same file: keep only the first (node_id, role)
        # hit. Consequence — if vertex X is reached from two different seed
        # ranges, only the FIRST seed's anchor_line is recorded on the
        # surviving QueriedNode; the second call site is invisible to the
        # caller. Acceptable for retrieval (each candidate once), surprising
        # for "every call-site" analysis (which graph_expand doesn't claim to
        # support — use raw `query_range` for that).
        seen: Set[Tuple[Optional[str], Optional[str]]] = set()
        deduped: List[QueriedNode] = []
        for n in results:
            key = (n.node_id, n.role)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(n)

        # Stable sort by role: defined first, then callees, then callers.
        deduped.sort(key=lambda n: _ROLE_ORDER.get(n.role or "", 99))

        return _truncate_with_hint(deduped, top_k)

    return execute
