# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codeminer.guardian.graph_diff (no external deps, no LLM)."""

import os

import pytest

from codeminer.graph.code_graph import CodeGraph
from codeminer.guardian.signals.graph_diff import (
    DriftSignal,
    EdgeChange,
    compute_drift_signals,
    diff_graphs,
    drift_findings,
    latest_snapshot_commit,
    load_snapshot,
    save_snapshot,
)
from codeminer.types import EDGE_TYPE_IMPORT, EDGE_TYPE_REFERENCE


# ---------------------------------------------------------------------------
# Helpers — build minimal CodeGraph fixtures
# ---------------------------------------------------------------------------

def _make_graph(edges: list) -> CodeGraph:
    """Build a CodeGraph from a list of (src, dst, edge_type) tuples.

    Each src/dst string is treated as both symbol name and file path for
    simplicity. Symbols are added with type "function".
    """
    g = CodeGraph()
    nodes = {name for (s, d, _) in edges for name in (s, d)}
    for name in nodes:
        g._add_vertex(name, {"type": "function", "file": f"{name}.py"})
    for src, dst, etype in edges:
        g._add_edge(src, dst, etype)
    return g


# ---------------------------------------------------------------------------
# diff_graphs
# ---------------------------------------------------------------------------

class TestDiffGraphs:
    def test_empty_graphs_produce_no_changes(self):
        prior = CodeGraph()
        current = CodeGraph()
        assert diff_graphs(prior, current) == []

    def test_added_edge_detected(self):
        prior = _make_graph([])
        current = _make_graph([("A", "B", EDGE_TYPE_REFERENCE)])
        changes = diff_graphs(prior, current)
        assert len(changes) == 1
        assert changes[0].kind == "added"
        assert changes[0].src == "A"
        assert changes[0].dst == "B"
        assert changes[0].edge_type == EDGE_TYPE_REFERENCE

    def test_removed_edge_detected(self):
        prior = _make_graph([("A", "B", EDGE_TYPE_IMPORT)])
        current = _make_graph([])
        changes = diff_graphs(prior, current)
        assert len(changes) == 1
        assert changes[0].kind == "removed"
        assert changes[0].edge_type == EDGE_TYPE_IMPORT

    def test_unchanged_edge_not_in_diff(self):
        prior = _make_graph([("A", "B", EDGE_TYPE_REFERENCE)])
        current = _make_graph([("A", "B", EDGE_TYPE_REFERENCE)])
        assert diff_graphs(prior, current) == []

    def test_contain_edges_ignored(self):
        prior = _make_graph([])
        current = _make_graph([("A", "B", "contain")])
        assert diff_graphs(prior, current) == []

    def test_multiple_changes(self):
        prior = _make_graph([("A", "B", EDGE_TYPE_REFERENCE)])
        current = _make_graph([
            ("A", "C", EDGE_TYPE_REFERENCE),
            ("A", "D", EDGE_TYPE_IMPORT),
        ])
        changes = diff_graphs(prior, current)
        kinds = {c.kind for c in changes}
        assert "added" in kinds
        assert "removed" in kinds
        added = [c for c in changes if c.kind == "added"]
        assert {(c.src, c.dst) for c in added} == {("A", "C"), ("A", "D")}


# ---------------------------------------------------------------------------
# compute_drift_signals
# ---------------------------------------------------------------------------

class TestComputeDriftSignals:
    def test_no_changes_no_signals(self):
        g = _make_graph([("A", "B", EDGE_TYPE_REFERENCE)])
        assert compute_drift_signals([], g, g) == []

    def test_fan_in_spike(self):
        prior = _make_graph([])
        # B gains 5 new callers
        current = _make_graph([
            (f"caller{i}", "B", EDGE_TYPE_REFERENCE) for i in range(5)
        ])
        changes = diff_graphs(prior, current)
        signals = compute_drift_signals(changes, current, prior, fan_in_threshold=5)
        spike = [s for s in signals if s.kind == "fan_in_spike"]
        assert len(spike) == 1
        assert spike[0].symbol == "B"
        assert "5" in spike[0].detail

    def test_fan_in_spike_below_threshold_not_emitted(self):
        prior = _make_graph([])
        current = _make_graph([
            (f"caller{i}", "B", EDGE_TYPE_REFERENCE) for i in range(3)
        ])
        changes = diff_graphs(prior, current)
        signals = compute_drift_signals(changes, current, prior, fan_in_threshold=5)
        assert not any(s.kind == "fan_in_spike" for s in signals)

    def test_contract_change(self):
        # A had 3 dependents in prior; now loses one outgoing edge → contract change
        prior = _make_graph([
            ("dep1", "A", EDGE_TYPE_REFERENCE),
            ("dep2", "A", EDGE_TYPE_REFERENCE),
            ("dep3", "A", EDGE_TYPE_REFERENCE),
            ("A", "B", EDGE_TYPE_REFERENCE),   # this edge gets removed
        ])
        current = _make_graph([
            ("dep1", "A", EDGE_TYPE_REFERENCE),
            ("dep2", "A", EDGE_TYPE_REFERENCE),
            ("dep3", "A", EDGE_TYPE_REFERENCE),
            # A→B removed
        ])
        changes = diff_graphs(prior, current)
        signals = compute_drift_signals(
            changes, current, prior, contract_min_prior_fan_in=3
        )
        contract = [s for s in signals if s.kind == "contract_change"]
        assert len(contract) == 1
        assert contract[0].symbol == "A"
        assert "3" in contract[0].detail

    def test_contract_change_below_threshold_not_emitted(self):
        prior = _make_graph([
            ("dep1", "A", EDGE_TYPE_REFERENCE),
            ("A", "B", EDGE_TYPE_REFERENCE),
        ])
        current = _make_graph([
            ("dep1", "A", EDGE_TYPE_REFERENCE),
        ])
        changes = diff_graphs(prior, current)
        signals = compute_drift_signals(
            changes, current, prior, contract_min_prior_fan_in=3
        )
        assert not any(s.kind == "contract_change" for s in signals)

    def test_private_symbol_skipped_for_api_surface(self):
        # _private should not emit api_surface_change
        prior = _make_graph([("dep", "_private", EDGE_TYPE_REFERENCE)])
        current = _make_graph([
            ("dep", "_private", EDGE_TYPE_REFERENCE),
            ("new_dep", "_private", EDGE_TYPE_REFERENCE),
        ])
        changes = diff_graphs(prior, current)
        signals = compute_drift_signals(changes, current, prior)
        assert not any(s.kind == "api_surface_change" for s in signals)


# ---------------------------------------------------------------------------
# drift_findings
# ---------------------------------------------------------------------------

class TestDriftFindings:
    def test_empty_signals_empty_findings(self):
        assert drift_findings([]) == []

    def test_each_signal_produces_one_finding(self):
        signals = [
            DriftSignal(kind="fan_in_spike", symbol="Foo", file="foo.py",
                        detail="Foo gained callers"),
            DriftSignal(kind="contract_change", symbol="Bar", file="bar.py",
                        detail="Bar lost edges"),
        ]
        findings = drift_findings(signals)
        assert len(findings) == 2
        for f in findings:
            assert f.kind == "drift"
        assert any("fan_in_spike" in f.title for f in findings)
        assert any("contract_change" in f.title for f in findings)


# ---------------------------------------------------------------------------
# Snapshot store (uses tmp_path fixture — no real file system side effects)
# ---------------------------------------------------------------------------

class TestSnapshotStore:
    def test_save_and_load_roundtrip(self, tmp_path):
        g = _make_graph([("A", "B", EDGE_TYPE_REFERENCE)])
        memory_dir = str(tmp_path)
        commit = "abc123def456"

        path = save_snapshot(g, memory_dir, commit)
        assert os.path.exists(path)
        assert path.endswith("abc123def456.pkl")

        loaded = load_snapshot(memory_dir, commit)
        assert loaded is not None
        # Edge should survive the pickle round-trip
        changes = diff_graphs(loaded, loaded)
        assert changes == []

    def test_load_missing_returns_none(self, tmp_path):
        result = load_snapshot(str(tmp_path), "nonexistent")
        assert result is None

    def test_latest_snapshot_commit(self, tmp_path):
        g = _make_graph([])
        memory_dir = str(tmp_path)
        assert latest_snapshot_commit(memory_dir) is None

        save_snapshot(g, memory_dir, "aaa111")
        save_snapshot(g, memory_dir, "zzz999")
        latest = latest_snapshot_commit(memory_dir)
        assert latest == "zzz999"

    def test_snapshot_truncates_commit_to_12_chars(self, tmp_path):
        g = _make_graph([])
        long_commit = "a" * 40
        path = save_snapshot(g, str(tmp_path), long_commit)
        assert "a" * 12 + ".pkl" in path
