# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Graph-diff drift signal for the Repository Guardian.

Compares two CodeGraph snapshots (current cycle vs. prior cycle) and surfaces
three deterministic signals — no LLM involved:

  fan_in_spike        — a symbol suddenly gained many new incoming edges
  contract_change     — a high-fan-in symbol lost outgoing edges (proxy for
                        arity / signature break with lagging dependents)
  api_surface_change  — a public symbol has net edge changes while none of its
                        existing dependents have new corresponding edges

The signals are converted to Finding(kind="drift") objects by drift_findings()
so the Guardian report pipeline sees them identically to churn findings.

Moved from codeminer.guardian.graph_diff (flat module) into the signals/
sub-package; relative imports updated accordingly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, FrozenSet, List, Optional, Tuple

from ...log_utils import get_logger
from ...types import DEPENDENCY_EDGE_TYPES, EDGE_TYPE_CONTAIN, is_symbol_node

if TYPE_CHECKING:
    from ...graph.code_graph import CodeGraph
    from ..report import Finding

logger = get_logger(__name__)

# Edge types we track for drift analysis (CONTAIN is structural, not semantic drift)
_TRACKED_EDGE_TYPES: FrozenSet[str] = DEPENDENCY_EDGE_TYPES


# ---------------------------------------------------------------------------
# Snapshot store
# ---------------------------------------------------------------------------

def _snapshot_path(memory_dir: str, commit: str) -> str:
    return os.path.join(memory_dir, "graph", f"{commit[:12]}.pkl")


def save_snapshot(graph: "CodeGraph", memory_dir: str, commit: str) -> str:
    """Save a CodeGraph snapshot keyed by commit SHA.

    Returns the path written.
    """
    path = _snapshot_path(memory_dir, commit)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    graph.save_graph(path)
    logger.debug("graph_diff: saved snapshot for %s → %s", commit[:12], path)
    return path


def load_snapshot(memory_dir: str, commit: str) -> "Optional[CodeGraph]":
    """Load the CodeGraph snapshot for a given commit SHA.

    Returns None if the snapshot does not exist.
    """
    from ...graph.code_graph import CodeGraph

    path = _snapshot_path(memory_dir, commit)
    if not os.path.exists(path):
        logger.debug("graph_diff: no snapshot for %s", commit[:12])
        return None
    try:
        graph = CodeGraph.load_graph(path)
        logger.debug("graph_diff: loaded snapshot for %s", commit[:12])
        return graph
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph_diff: failed to load snapshot %s: %s", path, exc)
        return None


def latest_snapshot_commit(memory_dir: str) -> Optional[str]:
    """Return the commit SHA of the most recently written snapshot, or None.

    Picks the lexicographically last .pkl filename (commit prefixes sort
    chronologically only if commits are ordered, so callers should prefer the
    memory store's cycle table; this is a fallback for offline use).
    """
    graph_dir = os.path.join(memory_dir, "graph")
    if not os.path.isdir(graph_dir):
        return None
    pkls = sorted(
        f for f in os.listdir(graph_dir) if f.endswith(".pkl")
    )
    if not pkls:
        return None
    return pkls[-1].replace(".pkl", "")


# ---------------------------------------------------------------------------
# Edge diff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeChange:
    """One added or removed dependency edge between two graph snapshots."""

    kind: str        # "added" | "removed"
    edge_type: str   # "reference" | "import" | "type-use"
    src: str         # source node name
    dst: str         # destination node name
    src_file: str    # file containing src (empty if unknown)
    dst_file: str    # file containing dst (empty if unknown)


def _edge_set(graph: "CodeGraph") -> FrozenSet[Tuple[str, str, str]]:
    """Return a frozenset of (src_name, dst_name, edge_type) for tracked edges."""
    ig = graph.graph
    triples = set()
    for edge in ig.es:
        etype = edge.attributes().get("type", "")
        if etype not in _TRACKED_EDGE_TYPES:
            continue
        src_name = ig.vs[edge.source]["name"]
        dst_name = ig.vs[edge.target]["name"]
        triples.add((src_name, dst_name, etype))
    return frozenset(triples)


def _node_file(graph: "CodeGraph", name: str) -> str:
    """Return the file attribute for a named node, or ''."""
    vertex = graph.name_to_vertex.get(name)
    if vertex is None:
        return ""
    attrs = graph.graph.vs[vertex].attributes()
    return attrs.get("file", "") or ""


def diff_graphs(prior: "CodeGraph", current: "CodeGraph") -> List[EdgeChange]:
    """Compute the symmetric edge difference between two CodeGraph snapshots.

    Returns a list of EdgeChange objects tagged "added" (present in current
    but not prior) or "removed" (present in prior but not current).
    """
    prior_edges = _edge_set(prior)
    current_edges = _edge_set(current)

    added = current_edges - prior_edges
    removed = prior_edges - current_edges

    changes: List[EdgeChange] = []

    for src, dst, etype in sorted(added):
        changes.append(EdgeChange(
            kind="added", edge_type=etype, src=src, dst=dst,
            src_file=_node_file(current, src),
            dst_file=_node_file(current, dst),
        ))

    for src, dst, etype in sorted(removed):
        changes.append(EdgeChange(
            kind="removed", edge_type=etype, src=src, dst=dst,
            src_file=_node_file(prior, src),
            dst_file=_node_file(prior, dst),
        ))

    logger.debug(
        "graph_diff: %d added, %d removed dependency edges",
        len(added), len(removed),
    )
    return changes


# ---------------------------------------------------------------------------
# Drift signals
# ---------------------------------------------------------------------------

@dataclass
class DriftSignal:
    """A human-readable structural drift finding derived from an edge diff."""

    kind: str                              # "fan_in_spike" | "contract_change" | "api_surface_change"
    symbol: str                            # primary symbol name
    file: str                              # file containing symbol
    detail: str                            # plain-English one-liner for report / LLM prompt
    edge_changes: List[EdgeChange] = field(default_factory=list)


def _fan_in_counts(
    graph: "CodeGraph",
    edge_types: FrozenSet[str] = _TRACKED_EDGE_TYPES,
) -> dict:
    """Return {node_name: incoming_edge_count} for all symbol nodes."""
    ig = graph.graph
    counts: dict = {}
    for edge in ig.es:
        if edge.attributes().get("type", "") not in edge_types:
            continue
        dst_name = ig.vs[edge.target]["name"]
        dst_type = ig.vs[edge.target].attributes().get("type", "")
        if not is_symbol_node(dst_type):
            continue
        counts[dst_name] = counts.get(dst_name, 0) + 1
    return counts


def compute_drift_signals(
    changes: List[EdgeChange],
    current: "CodeGraph",
    prior: "CodeGraph",
    fan_in_threshold: int = 5,
    contract_min_prior_fan_in: int = 3,
) -> List[DriftSignal]:
    """Derive DriftSignal list from a raw EdgeChange list.

    Three signal types:

    fan_in_spike
        A symbol gained >= fan_in_threshold new incoming dependency edges.
        Signals rising blast-radius: many new callers/importers of an interface.

    contract_change
        A symbol that had >= contract_min_prior_fan_in prior dependents lost
        one or more outgoing dependency edges.  Proxy for arity / signature
        change with lagging dependents.

    api_surface_change
        A public symbol (name contains no '.'-prefixed component starting with
        '_') has net edge changes while none of its existing prior dependents
        gained new corresponding outgoing edges — suggesting the contract
        changed but consumers haven't caught up.
    """
    if not changes:
        return []

    # --- fan_in_spike ---
    added_in: dict = {}   # dst → [EdgeChange]
    removed_out: dict = {}  # src → [EdgeChange]

    for ch in changes:
        if ch.kind == "added":
            added_in.setdefault(ch.dst, []).append(ch)
            removed_out  # just ensure key exists below if needed
        else:
            removed_out.setdefault(ch.src, []).append(ch)

    signals: List[DriftSignal] = []

    # fan_in_spike: dst gained many new incoming edges
    for dst, chs in added_in.items():
        if len(chs) >= fan_in_threshold:
            signals.append(DriftSignal(
                kind="fan_in_spike",
                symbol=dst,
                file=chs[0].dst_file,
                detail=(
                    f"`{dst}` gained {len(chs)} new incoming "
                    f"{chs[0].edge_type} edge(s) — rising blast-radius."
                ),
                edge_changes=chs,
            ))

    # --- contract_change ---
    prior_fan_in = _fan_in_counts(prior)

    for src, chs in removed_out.items():
        prior_fi = prior_fan_in.get(src, 0)
        if prior_fi >= contract_min_prior_fan_in:
            signals.append(DriftSignal(
                kind="contract_change",
                symbol=src,
                file=chs[0].src_file,
                detail=(
                    f"`{src}` lost {len(chs)} outgoing edge(s); "
                    f"it had {prior_fi} dependent(s) in the prior snapshot — "
                    f"possible contract / signature change with lagging consumers."
                ),
                edge_changes=chs,
            ))

    # --- api_surface_change ---
    # Public symbols that have net changes; check if their prior dependents
    # added any outgoing edge toward them (if not, consumers haven't adapted).
    current_fan_in = _fan_in_counts(current)

    changed_symbols: set = set()
    for ch in changes:
        changed_symbols.add(ch.dst if ch.kind == "added" else ch.src)

    # Collect all prior dependents of each changed symbol
    prior_ig = prior.graph
    prior_dependents: dict = {}  # symbol → {dependent_name}
    for edge in prior_ig.es:
        if edge.attributes().get("type", "") not in _TRACKED_EDGE_TYPES:
            continue
        dst_name = prior_ig.vs[edge.target]["name"]
        if dst_name in changed_symbols:
            src_name = prior_ig.vs[edge.source]["name"]
            prior_dependents.setdefault(dst_name, set()).add(src_name)

    # Collect new outgoing edges from those dependents in current
    current_ig = current.graph
    new_dependent_edges: dict = {}  # symbol → {dependent_name} that added an edge
    for edge in current_ig.es:
        if edge.attributes().get("type", "") not in _TRACKED_EDGE_TYPES:
            continue
        dst_name = current_ig.vs[edge.target]["name"]
        src_name = current_ig.vs[edge.source]["name"]
        if dst_name in changed_symbols:
            # Check if this (src→dst) was absent in prior
            prior_edges_for_dst = {
                prior_ig.vs[e.source]["name"]
                for e in prior_ig.es
                if prior_ig.vs[e.target]["name"] == dst_name
                and e.attributes().get("type", "") in _TRACKED_EDGE_TYPES
            }
            if src_name not in prior_edges_for_dst:
                new_dependent_edges.setdefault(dst_name, set()).add(src_name)

    for sym in changed_symbols:
        # public symbol heuristic: no component of the qualified name starts with _
        parts = sym.split(".")
        if any(p.startswith("_") for p in parts):
            continue
        prior_deps = prior_dependents.get(sym, set())
        if not prior_deps:
            continue  # no prior dependents — not interesting
        adapted = new_dependent_edges.get(sym, set())
        lagging = prior_deps - adapted
        if lagging:
            n_lag = len(lagging)
            sym_changes = [c for c in changes if c.src == sym or c.dst == sym]
            f = _node_file(current, sym) or _node_file(prior, sym)
            signals.append(DriftSignal(
                kind="api_surface_change",
                symbol=sym,
                file=f,
                detail=(
                    f"`{sym}` interface changed; "
                    f"{n_lag} prior dependent(s) have not added new edges — "
                    f"possible undetected breakage."
                ),
                edge_changes=sym_changes,
            ))

    logger.info(
        "graph_diff: %d drift signal(s): %s",
        len(signals),
        ", ".join(s.kind for s in signals),
    )
    return signals


# ---------------------------------------------------------------------------
# Convert to Finding objects
# ---------------------------------------------------------------------------

def drift_findings(signals: List[DriftSignal]) -> "List[Finding]":
    """Convert DriftSignal list to guardian Finding objects for the report."""
    from ..report import Finding

    findings = []
    for sig in signals:
        loc = f"{sig.file}: " if sig.file else ""
        findings.append(Finding(
            kind="drift",
            title=f"Graph-diff drift: {sig.kind} — {loc}`{sig.symbol}`",
            detail=sig.detail,
        ))
    return findings
