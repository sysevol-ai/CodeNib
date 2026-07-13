# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codeminer.guardian.memory (SQLite store, no LLM, no network)."""

import pytest

from codeminer.graph.code_graph import CodeGraph
from codeminer.guardian.memory import MemoryStore, format_findings_for_prompt
from codeminer.guardian.report import Finding, GuardianReport
from codeminer.types import EDGE_TYPE_REFERENCE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_report(commit: str = "abc123", findings=None) -> GuardianReport:
    return GuardianReport(
        repo="/tmp/repo",
        commit=commit,
        generated_at="2026-07-08 00:00:00 UTC",
        churn_window="90 days ago",
        findings=findings or [],
    )


def _make_graph() -> CodeGraph:
    g = CodeGraph()
    g._add_vertex("A", {"type": "function", "file": "a.py"})
    g._add_vertex("B", {"type": "function", "file": "b.py"})
    g._add_edge("A", "B", EDGE_TYPE_REFERENCE)
    return g


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_db_file(self, tmp_path):
        store = MemoryStore(str(tmp_path / "mem"))
        assert (tmp_path / "mem" / "index.sqlite").exists()

    def test_readonly_does_not_create_db(self, tmp_path):
        store = MemoryStore(str(tmp_path / "mem"), readonly=True)
        assert not (tmp_path / "mem").exists()

    def test_cycle_count_zero_on_fresh_store(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.cycle_count() == 0


# ---------------------------------------------------------------------------
# persist_cycle
# ---------------------------------------------------------------------------

class TestPersistCycle:
    def test_returns_cycle_no(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        no = store.persist_cycle(_make_report("c1"))
        assert no == 1

    def test_increments_cycle_no(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        n1 = store.persist_cycle(_make_report("c1"))
        n2 = store.persist_cycle(_make_report("c2"))
        assert n2 == n1 + 1

    def test_cycle_count_increments(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(_make_report("c1"))
        store.persist_cycle(_make_report("c2"))
        assert store.cycle_count() == 2

    def test_findings_persisted(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        findings = [
            Finding(kind="churn", title="High-churn file: foo.py", detail="Changed 5 times"),
            Finding(kind="drift", title="Graph-diff drift: fan_in_spike — bar.py: `Baz`"),
        ]
        store.persist_cycle(_make_report("c1", findings))
        rows = store.recent_findings(k=10)
        assert len(rows) == 2
        kinds = {r["kind"] for r in rows}
        assert kinds == {"churn", "drift"}

    def test_readonly_persist_is_noop(self, tmp_path):
        store = MemoryStore(str(tmp_path), readonly=True)
        result = store.persist_cycle(_make_report("c1"))
        assert result == -1
        assert store.cycle_count() == 0

    def test_graph_saved_and_has_graph_flag_set(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        g = _make_graph()
        store.persist_cycle(_make_report("abc123def456"), graph=g)
        cycles = store.recent_cycles(k=1)
        assert cycles[0]["has_graph"] == 1
        assert (tmp_path / "graph" / "abc123def456.pkl").exists()

    def test_no_graph_has_graph_flag_zero(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(_make_report("c1"))
        cycles = store.recent_cycles(k=1)
        assert cycles[0]["has_graph"] == 0

    def test_token_cost_stored(self, tmp_path):
        from codeminer.guardian.investigator import LLMUsage
        usage = LLMUsage()
        usage.prompt_tokens = 100
        usage.completion_tokens = 50
        usage.total_tokens = 150
        report = _make_report("c1")
        report.llm_usage = usage
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(report)
        cycles = store.recent_cycles(k=1)
        assert cycles[0]["token_cost"] == 150


# ---------------------------------------------------------------------------
# recent_findings
# ---------------------------------------------------------------------------

class TestRecentFindings:
    def test_empty_on_no_cycles(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.recent_findings() == []

    def test_returns_newest_first(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(_make_report("c1", [
            Finding(kind="churn", title="High-churn file: old.py")
        ]))
        store.persist_cycle(_make_report("c2", [
            Finding(kind="churn", title="High-churn file: new.py")
        ]))
        rows = store.recent_findings(k=5)
        assert rows[0]["title"] == "High-churn file: new.py"


    def test_k_limits_results(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        for i in range(10):
            store.persist_cycle(_make_report(f"c{i}", [
                Finding(kind="churn", title=f"High-churn file: f{i}.py")
            ]))
        rows = store.recent_findings(k=3)
        assert len(rows) == 3

    def test_readonly_returns_empty(self, tmp_path):
        store = MemoryStore(str(tmp_path), readonly=True)
        assert store.recent_findings() == []


# ---------------------------------------------------------------------------
# load_prior_graph
# ---------------------------------------------------------------------------

class TestLoadPriorGraph:
    def test_returns_none_when_no_graph_saved(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(_make_report("c1"))
        assert store.load_prior_graph() is None

    def test_loads_saved_graph(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        g = _make_graph()
        store.persist_cycle(_make_report("abc123def456"), graph=g)
        loaded = store.load_prior_graph()
        assert loaded is not None

    def test_loads_most_recent_graph(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        g1 = _make_graph()
        store.persist_cycle(_make_report("aaa111"), graph=g1)
        store.persist_cycle(_make_report("bbb222"), graph=None)  # no graph
        g3 = _make_graph()
        store.persist_cycle(_make_report("ccc333"), graph=g3)
        loaded = store.load_prior_graph()
        assert loaded is not None  # should be ccc333's graph

    def test_readonly_returns_none(self, tmp_path):
        store = MemoryStore(str(tmp_path), readonly=True)
        assert store.load_prior_graph() is None


# ---------------------------------------------------------------------------
# symbol_history + edge_drift
# ---------------------------------------------------------------------------

class TestSymbolHistory:
    def test_unknown_symbol_returns_empty(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        assert store.symbol_history("nonexistent") == {}

    def test_symbol_recorded_after_persist(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(_make_report("c1", [
            Finding(kind="churn", title="High-churn file: codeminer/foo.py")
        ]))
        h = store.symbol_history("codeminer/foo.py")
        assert h["first_seen_cycle"] == 1
        assert h["last_seen_cycle"] == 1


class TestEdgeDrift:
    def test_empty_on_no_edges(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(_make_report("c1"))
        assert store.edge_drift(since_cycle=1) == []

    def test_persisted_edges_returned(self, tmp_path):
        from codeminer.guardian.signals.graph_diff import EdgeChange
        store = MemoryStore(str(tmp_path))
        no = store.persist_cycle(_make_report("c1"))
        changes = [EdgeChange(
            kind="added", edge_type=EDGE_TYPE_REFERENCE,
            src="A", dst="B", src_file="a.py", dst_file="b.py",
        )]
        store.persist_edge_changes(no, changes)
        drift = store.edge_drift(since_cycle=1)
        assert len(drift) == 1
        assert drift[0]["src_symbol"] == "A"


# ---------------------------------------------------------------------------
# assert_cross_cycle (M2 invariant)
# ---------------------------------------------------------------------------

class TestCrossCycleAssertion:
    def test_fails_with_zero_cycles(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        with pytest.raises(AssertionError, match="cycles"):
            store.assert_cross_cycle()

    def test_fails_with_one_cycle(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(_make_report("c1", [
            Finding(kind="churn", title="High-churn file: f.py")
        ]))
        with pytest.raises(AssertionError):
            store.assert_cross_cycle()

    def test_passes_with_two_cycles_and_findings(self, tmp_path):
        store = MemoryStore(str(tmp_path))
        store.persist_cycle(_make_report("c1", [
            Finding(kind="churn", title="High-churn file: f.py")
        ]))
        store.persist_cycle(_make_report("c2", [
            Finding(kind="drift", title="Graph-diff drift: contract_change — `foo`")
        ]))
        store.assert_cross_cycle()  # must not raise


# ---------------------------------------------------------------------------
# format_findings_for_prompt
# ---------------------------------------------------------------------------

class TestFormatFindingsForPrompt:
    def test_empty_findings(self):
        assert "no prior findings" in format_findings_for_prompt([])

    def test_renders_title_and_commit(self):
        findings = [{"commit": "abc12345", "kind": "churn",
                     "title": "High-churn file: foo.py", "detail": "5 commits"}]
        out = format_findings_for_prompt(findings)
        assert "abc12345" in out  # commit_sha field used by format helper
        assert "churn" in out
        assert "foo.py" in out

    def test_truncates_to_max_findings(self):
        findings = [
            {"commit": f"c{i}" * 4, "kind": "churn",
             "title": f"f{i}.py", "detail": ""}
            for i in range(10)
        ]
        out = format_findings_for_prompt(findings, max_findings=3)
        assert "7 more" in out
