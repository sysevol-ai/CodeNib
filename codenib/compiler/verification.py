# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Admission control for incrementally updated indexes.

An incremental update is only *admissible* when the patched artifact is
equivalent to what a fresh rebuild would have produced. This module defines
that check as a pluggable contract so the compiler can decide, per update,
whether to keep the patched result or discard it and rebuild.

Three implementations ship here:

``NullVerifier``
    Cannot prove anything, and says so. Every result comes back
    ``verified=False`` with ``reason="no verifier configured"``. This is the
    default: it makes "we did not check" explicit rather than silent.

``AlwaysAdmitVerifier``
    Admits without checking. Only for environments that have accepted the
    risk deliberately -- it exists so that choice is a visible configuration
    value rather than a missing code path.

``FactProjectionVerifier``
    Compares the facts a patched graph states with those of a fresh build.
    This is the whole-graph half of the exactness check.

The serving-replay half of the exactness check lives
in the evaluation harness and is intended to drop in here as another
implementation without changing any caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass(slots=True)
class VerificationResult:
    """Outcome of checking a patched artifact against its fresh target.

    Attributes:
        verified: True only when the patched artifact was *proven* equivalent.
            False covers both "checked and differs" and "could not check".
        reason: Human-readable explanation, always populated when not verified.
        checked: Whether a real comparison ran at all. Distinguishes "no
            verifier available" (``checked=False``) from "compared and found
            different" (``checked=True, verified=False``) -- these have very
            different implications and should never be conflated in reporting.
        details: Optional verifier-specific metrics (edge F1, mismatch counts).
    """

    verified: bool
    reason: str = ""
    checked: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> Dict[str, Any]:
        """Flatten into manifest metadata so admission is auditable on disk."""
        meta: Dict[str, Any] = {
            "verified": self.verified,
            "verification_checked": self.checked,
        }
        if self.reason:
            meta["verification_reason"] = self.reason
        if self.details:
            meta["verification_details"] = dict(self.details)
        return meta


@runtime_checkable
class UpdateVerifier(Protocol):
    """Decides whether a patched artifact may be kept.

    Implementations must be side-effect free: returning ``verified=False``
    tells the caller to discard the patch and rebuild, so a verifier that
    mutated state would corrupt that fallback.
    """

    def verify(
        self,
        *,
        index_type: str,
        patched: Any,
        fresh: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        """Check *patched*, optionally against a freshly built *fresh*."""
        ...


class NullVerifier:
    """Default verifier: proves nothing and reports that honestly."""

    name = "null"

    def verify(
        self,
        *,
        index_type: str,
        patched: Any,
        fresh: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        return VerificationResult(
            verified=False,
            checked=False,
            reason="no verifier configured; incremental result unverified",
        )


class AlwaysAdmitVerifier:
    """Admits every update without checking.

    Use only where the correctness risk has been accepted knowingly. The
    result still records ``checked=False`` so downstream reporting cannot
    mistake this for a proof.
    """

    name = "always-admit"

    def verify(
        self,
        *,
        index_type: str,
        patched: Any,
        fresh: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        return VerificationResult(
            verified=True,
            checked=False,
            reason="admitted without verification by configuration",
        )


def graph_facts(graph: Any) -> tuple[Any, Any]:
    """The facts a symbol graph states, as two multisets.

    A vertex is its unified name, type, file, definition range and selection
    line; a vertex with no source range is its unified name and type. An edge
    is the unified names of its ends, its type and its anchor. Identity is the
    unified name, not the vertex key: a patched graph and a fresh one may key
    the same definition differently.
    """

    from collections import Counter

    core = getattr(graph, "graph", graph)
    vs = core.vs
    attributes = vs.attributes()
    names = vs["name"]
    unified = (
        [value or name for value, name in zip(vs["unified_name"], names, strict=True)]
        if "unified_name" in attributes
        else list(names)
    )
    selection = (
        vs["selection_line"] if "selection_line" in attributes else [None] * len(names)
    )
    vertices: Counter = Counter()
    for index, vertex in enumerate(vs):
        if vertex["file"] and vertex["start_line"] is not None:
            vertices[
                (
                    unified[index],
                    vertex["type"],
                    vertex["file"],
                    vertex["start_line"],
                    vertex["end_line"],
                    selection[index],
                )
            ] += 1
        else:
            vertices[(unified[index], vertex["type"])] += 1
    es = core.es
    edge_attributes = es.attributes()
    count = core.ecount()
    files = es["anchor_file"] if "anchor_file" in edge_attributes else [None] * count
    lines = es["anchor_line"] if "anchor_line" in edge_attributes else [None] * count
    edges: Counter = Counter()
    for (source, target), kind, file, line in zip(
        core.get_edgelist(), es["type"], files, lines, strict=True
    ):
        edges[(unified[source], unified[target], kind, file, line)] += 1
    return vertices, edges


class FactProjectionVerifier:
    """Admits a patched graph only when it states the facts of a fresh one.

    This is the whole-graph half of the exactness check: vertex facts and
    anchored-edge facts compared as multisets (see :func:`graph_facts`). It
    needs the fresh graph, so it measures a patch rather than saving a build.
    It does not replay definition and reference requests against the
    persisted graph; a graph it admits has not passed that half.
    """

    name = "fact-projection"

    def verify(
        self,
        *,
        index_type: str,
        patched: Any,
        fresh: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> VerificationResult:
        if fresh is None:
            return VerificationResult(
                verified=False,
                checked=False,
                reason="no fresh graph to compare with",
            )
        patched_vertices, patched_edges = graph_facts(patched)
        fresh_vertices, fresh_edges = graph_facts(fresh)
        details = {
            "vertices": sum(fresh_vertices.values()),
            "edges": sum(fresh_edges.values()),
            "vertices_missing": sum((fresh_vertices - patched_vertices).values()),
            "vertices_extra": sum((patched_vertices - fresh_vertices).values()),
            "edges_missing": sum((fresh_edges - patched_edges).values()),
            "edges_extra": sum((patched_edges - fresh_edges).values()),
        }
        differing = sum(value for key, value in details.items() if "_" in key)
        return VerificationResult(
            verified=differing == 0,
            checked=True,
            reason="" if differing == 0 else f"{differing} graph facts differ",
            details=details,
        )
