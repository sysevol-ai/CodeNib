# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stable graph signatures for decoder parity and backend alignment tests."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

VERTEX_COMPARED_ATTRS = ("type", "file", "start_line", "end_line", "unified_name")
EDGE_COMPARED_ATTRS = ("type", "anchor_file", "anchor_line")

EdgeSignature = Tuple[str, str, Optional[str], Optional[str], Optional[int]]


@dataclass(frozen=True, slots=True)
class VertexAttrMismatch:
    """A differing vertex attribute in two graph signatures."""

    name: str
    attr: str
    reference: Any
    candidate: Any


@dataclass(frozen=True, slots=True)
class EdgeCountDelta:
    """Multiplicity delta for one edge signature."""

    edge: EdgeSignature
    reference_count: int
    candidate_count: int


@dataclass(frozen=True, slots=True)
class GraphSignature:
    """Order-independent CodeGraph schema signature."""

    names: frozenset[str]
    edges: Tuple[EdgeSignature, ...]
    vertex_attrs: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class GraphSignatureDiff:
    """Difference between two graph signatures."""

    missing_names: Tuple[str, ...]
    extra_names: Tuple[str, ...]
    missing_edges: Tuple[EdgeSignature, ...]
    extra_edges: Tuple[EdgeSignature, ...]
    edge_count_deltas: Tuple[EdgeCountDelta, ...]
    vertex_attr_mismatches: Tuple[VertexAttrMismatch, ...]

    @property
    def is_empty(self) -> bool:
        return not (
            self.missing_names
            or self.extra_names
            or self.missing_edges
            or self.extra_edges
            or self.edge_count_deltas
            or self.vertex_attr_mismatches
        )


@dataclass(frozen=True, slots=True)
class GraphAlignmentTolerance:
    """Explicit tolerances for cross-backend graph alignment.

    Serial/core parity should use the default strict tolerance. Future LSP-vs-
    SCIP harnesses can relax only the quantities a server is allowed to differ
    on, while keeping schema attributes and line conventions visible.
    """

    max_missing_names: int = 0
    max_extra_names: int = 0
    max_missing_edges: int = 0
    max_extra_edges: int = 0
    max_edge_count_deltas: int = 0
    max_vertex_attr_mismatches: int = 0


def _unwrap_graph(graph_or_code_graph):
    return getattr(graph_or_code_graph, "graph", graph_or_code_graph)


def _edge_sort_key(edge: EdgeSignature):
    src, tgt, edge_type, anchor_file, anchor_line = edge
    return (
        src,
        tgt,
        edge_type or "",
        anchor_file or "",
        -1 if anchor_line is None else anchor_line,
    )


def graph_signature(graph_or_code_graph) -> GraphSignature:
    """Return the graph contract signature used by backend comparison tests."""

    graph = _unwrap_graph(graph_or_code_graph)
    names = frozenset(v["name"] for v in graph.vs)
    index_to_name = {v.index: v["name"] for v in graph.vs}
    edges: list[EdgeSignature] = []
    for edge in graph.es:
        attrs = edge.attributes()
        edges.append(
            (
                index_to_name[edge.source],
                index_to_name[edge.target],
                attrs.get("type"),
                attrs.get("anchor_file"),
                attrs.get("anchor_line"),
            )
        )
    edges.sort(key=_edge_sort_key)
    vertex_attrs = {
        v["name"]: {attr: v.attributes().get(attr) for attr in VERTEX_COMPARED_ATTRS}
        for v in graph.vs
    }
    return GraphSignature(names=names, edges=tuple(edges), vertex_attrs=vertex_attrs)


def diff_graph_signatures(
    reference: GraphSignature,
    candidate: GraphSignature,
) -> GraphSignatureDiff:
    """Return an order-independent diff between two graph signatures."""

    ref_counts = Counter(reference.edges)
    cand_counts = Counter(candidate.edges)
    missing_edges = tuple(
        sorted((ref_counts - cand_counts).elements(), key=_edge_sort_key)
    )
    extra_edges = tuple(
        sorted((cand_counts - ref_counts).elements(), key=_edge_sort_key)
    )

    edge_count_deltas: list[EdgeCountDelta] = []
    for edge in sorted(set(ref_counts) | set(cand_counts), key=_edge_sort_key):
        ref_count = ref_counts.get(edge, 0)
        cand_count = cand_counts.get(edge, 0)
        if ref_count != cand_count:
            edge_count_deltas.append(EdgeCountDelta(edge, ref_count, cand_count))

    shared_names = reference.names & candidate.names
    attr_mismatches: list[VertexAttrMismatch] = []
    for name in sorted(shared_names):
        ref_attrs = reference.vertex_attrs[name]
        cand_attrs = candidate.vertex_attrs[name]
        for attr in VERTEX_COMPARED_ATTRS:
            if ref_attrs.get(attr) != cand_attrs.get(attr):
                attr_mismatches.append(
                    VertexAttrMismatch(
                        name=name,
                        attr=attr,
                        reference=ref_attrs.get(attr),
                        candidate=cand_attrs.get(attr),
                    )
                )

    return GraphSignatureDiff(
        missing_names=tuple(sorted(reference.names - candidate.names)),
        extra_names=tuple(sorted(candidate.names - reference.names)),
        missing_edges=missing_edges,
        extra_edges=extra_edges,
        edge_count_deltas=tuple(edge_count_deltas),
        vertex_attr_mismatches=tuple(attr_mismatches),
    )


def _summary(diff: GraphSignatureDiff, tag: str) -> str:
    parts = [
        f"[{tag}] graph signatures differ",
        f"missing_names={len(diff.missing_names)}",
        f"extra_names={len(diff.extra_names)}",
        f"missing_edges={len(diff.missing_edges)}",
        f"extra_edges={len(diff.extra_edges)}",
        f"edge_count_deltas={len(diff.edge_count_deltas)}",
        f"vertex_attr_mismatches={len(diff.vertex_attr_mismatches)}",
    ]
    if diff.missing_names:
        parts.append(f"sample_missing_names={diff.missing_names[:3]}")
    if diff.extra_names:
        parts.append(f"sample_extra_names={diff.extra_names[:3]}")
    if diff.edge_count_deltas:
        parts.append(f"sample_edge_deltas={diff.edge_count_deltas[:3]}")
    if diff.vertex_attr_mismatches:
        parts.append(f"sample_attr_mismatches={diff.vertex_attr_mismatches[:3]}")
    return "; ".join(parts)


def assert_graph_signatures_equal(reference, candidate, tag: str = "graph") -> None:
    """Assert exact graph parity using the CodeGraph schema contract."""

    diff = diff_graph_signatures(graph_signature(reference), graph_signature(candidate))
    if not diff.is_empty:
        raise AssertionError(_summary(diff, tag))


def assert_graph_alignment_within_tolerance(
    reference,
    candidate,
    tolerance: GraphAlignmentTolerance,
    tag: str = "graph",
) -> None:
    """Assert graph alignment using explicit, counted tolerances."""

    diff = diff_graph_signatures(graph_signature(reference), graph_signature(candidate))
    violations = []
    if len(diff.missing_names) > tolerance.max_missing_names:
        violations.append("missing_names")
    if len(diff.extra_names) > tolerance.max_extra_names:
        violations.append("extra_names")
    if len(diff.missing_edges) > tolerance.max_missing_edges:
        violations.append("missing_edges")
    if len(diff.extra_edges) > tolerance.max_extra_edges:
        violations.append("extra_edges")
    if len(diff.edge_count_deltas) > tolerance.max_edge_count_deltas:
        violations.append("edge_count_deltas")
    if len(diff.vertex_attr_mismatches) > tolerance.max_vertex_attr_mismatches:
        violations.append("vertex_attr_mismatches")

    if violations:
        raise AssertionError(f"{_summary(diff, tag)}; violations={violations}")
