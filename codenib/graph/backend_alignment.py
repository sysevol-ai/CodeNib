# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Backend-alignment checks for CodeGraph-producing indexers.

Serial/core parity is intentionally strict and bit-for-bit.  Backend alignment
is different: SCIP, clangd, and generic LSP backends may use different internal
vertex identities, but their definition surface should agree on readable
``unified_name`` symbols and containment hierarchy before reference edges are
trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FILE,
    is_symbol_node,
    node_has_definition,
)
from .code_graph import CodeGraph


@dataclass(frozen=True, slots=True)
class BackendAlignmentTolerances:
    """Allowed graph-surface differences between two backends."""

    max_missing_symbols: int = 0
    max_extra_symbols: int = 0
    max_missing_containment: int = 0
    max_extra_containment: int = 0


@dataclass(frozen=True, slots=True)
class BackendAlignmentReport:
    """Structured result for backend-alignment comparison."""

    reference_symbols: int
    candidate_symbols: int
    missing_symbols: tuple[str, ...]
    extra_symbols: tuple[str, ...]
    reference_containment: int
    candidate_containment: int
    missing_containment: tuple[tuple[str, str], ...]
    extra_containment: tuple[tuple[str, str], ...]
    reference_references: int
    candidate_references: int
    tolerances: BackendAlignmentTolerances

    @property
    def ok(self) -> bool:
        t = self.tolerances
        return (
            len(self.missing_symbols) <= t.max_missing_symbols
            and len(self.extra_symbols) <= t.max_extra_symbols
            and len(self.missing_containment) <= t.max_missing_containment
            and len(self.extra_containment) <= t.max_extra_containment
        )

    def summary(self, *, sample: int = 5) -> str:
        status = "ok" if self.ok else "mismatch"
        return (
            f"backend alignment {status}: "
            f"symbols ref={self.reference_symbols} cand={self.candidate_symbols} "
            f"missing={len(self.missing_symbols)} extra={len(self.extra_symbols)}; "
            f"contain ref={self.reference_containment} "
            f"cand={self.candidate_containment} "
            f"missing={len(self.missing_containment)} "
            f"extra={len(self.extra_containment)}; "
            f"refs ref={self.reference_references} "
            f"cand={self.candidate_references}; "
            f"sample_missing_symbols={self.missing_symbols[:sample]} "
            f"sample_extra_symbols={self.extra_symbols[:sample]}"
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reference_symbols": self.reference_symbols,
            "candidate_symbols": self.candidate_symbols,
            "missing_symbols": list(self.missing_symbols),
            "extra_symbols": list(self.extra_symbols),
            "reference_containment": self.reference_containment,
            "candidate_containment": self.candidate_containment,
            "missing_containment": [list(edge) for edge in self.missing_containment],
            "extra_containment": [list(edge) for edge in self.extra_containment],
            "reference_references": self.reference_references,
            "candidate_references": self.candidate_references,
            "tolerances": {
                "max_missing_symbols": self.tolerances.max_missing_symbols,
                "max_extra_symbols": self.tolerances.max_extra_symbols,
                "max_missing_containment": self.tolerances.max_missing_containment,
                "max_extra_containment": self.tolerances.max_extra_containment,
            },
        }


def compare_backend_graphs(
    reference: CodeGraph,
    candidate: CodeGraph,
    tolerances: Optional[BackendAlignmentTolerances] = None,
) -> BackendAlignmentReport:
    """Compare two backend graphs on the stable definition surface."""

    tolerances = tolerances or BackendAlignmentTolerances()

    ref_symbols = _definition_symbols(reference)
    cand_symbols = _definition_symbols(candidate)
    ref_contain = _definition_containment_edges(reference)
    cand_contain = _definition_containment_edges(candidate)

    return BackendAlignmentReport(
        reference_symbols=len(ref_symbols),
        candidate_symbols=len(cand_symbols),
        missing_symbols=tuple(sorted(ref_symbols - cand_symbols)),
        extra_symbols=tuple(sorted(cand_symbols - ref_symbols)),
        reference_containment=len(ref_contain),
        candidate_containment=len(cand_contain),
        missing_containment=tuple(sorted(ref_contain - cand_contain)),
        extra_containment=tuple(sorted(cand_contain - ref_contain)),
        reference_references=_edge_count(reference, EDGE_TYPE_REFERENCE),
        candidate_references=_edge_count(candidate, EDGE_TYPE_REFERENCE),
        tolerances=tolerances,
    )


def _definition_symbols(graph: CodeGraph) -> set[str]:
    result: set[str] = set()
    for vertex in graph.graph.vs:
        key = _definition_key(vertex)
        if key:
            result.add(key)
    return result


def _definition_containment_edges(graph: CodeGraph) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    vertices = graph.graph.vs
    for edge in graph.graph.es:
        if edge.attributes().get("type") != EDGE_TYPE_CONTAIN:
            continue
        target = _definition_key(vertices[edge.target])
        if not target:
            continue
        source = _node_key(vertices[edge.source])
        if source:
            edges.add((source, target))
    return edges


def _definition_key(vertex) -> Optional[str]:
    attrs = vertex.attributes()
    if not is_symbol_node(attrs.get("type", "")):
        return None
    if not node_has_definition(attrs):
        return None
    return attrs.get("unified_name") or vertex["name"]


def _node_key(vertex) -> Optional[str]:
    attrs = vertex.attributes()
    node_type = attrs.get("type")
    if is_symbol_node(node_type):
        return _definition_key(vertex)
    if node_type == NODE_TYPE_FILE:
        return vertex["name"]
    return None


def _edge_count(graph: CodeGraph, edge_type: str) -> int:
    return sum(
        1 for edge in graph.graph.es if edge.attributes().get("type") == edge_type
    )


__all__ = [
    "BackendAlignmentReport",
    "BackendAlignmentTolerances",
    "compare_backend_graphs",
]
