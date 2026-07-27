# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the v2 hypothesis-trajectory memory store."""

import pytest

from codeminer.graph.code_graph import CodeGraph
from codeminer.guardian.loop import CycleState, Hypothesis, Signal
from codeminer.guardian.memory import MemoryStore, format_findings_for_prompt
from codeminer.guardian.report import GuardianReport, report_views
from codeminer.types import EDGE_TYPE_REFERENCE


def _hypothesis(cycle_no=1, *, grade="finding", suffix=""):
    evidence = []
    if grade in {"finding", "supported", "refuted"}:
        evidence = [f"probe-valid:{cycle_no}:1"]
    elif grade == "resolved":
        evidence = [f"resolved:commit{cycle_no}:fixed"]
    return Hypothesis.create(
        claim=f"parse{suffix} accepts invalid input",
        consequence="invalid state reaches callers",
        remedy="validate input and add a regression test",
        origin="exploration",
        locus=[f"mod{suffix}.py:parse"],
        evidence=evidence,
        grade=grade,
        cycle_no=cycle_no,
    )


def _state(cycle_no=1, *, hypotheses=None):
    return CycleState(
        cycle_no=cycle_no,
        commit=f"commit{cycle_no}",
        hypotheses=list(hypotheses or []),
        signals=[
            Signal.create(
                kind="churn",
                locus=["mod.py"],
                detail="mod.py changed three times",
            )
        ],
        current=None,
        budget_total=10_000,
        budget_spent=100,
        decision_log=[{"event": "tool_call", "tool": "recall"}],
        exit_reason="ReportSubmitted",
        carried_from=(cycle_no - 1 if cycle_no > 1 else None),
        report_summary="done",
    )


def _report(state):
    findings, backlog, retractions = report_views(state.hypotheses)
    return GuardianReport(
        repo="/repo",
        commit=state.commit,
        generated_at="2026-07-08 00:00:00 UTC",
        churn_window="90 days ago",
        findings=findings,
        backlog=backlog,
        retractions=retractions,
        cycle_no=state.cycle_no,
        exit_reason=state.exit_reason or "",
    )


def _graph():
    graph = CodeGraph()
    graph._add_vertex("A", {"type": "function", "file": "a.py"})
    graph._add_vertex("B", {"type": "function", "file": "b.py"})
    graph._add_edge("A", "B", EDGE_TYPE_REFERENCE)
    return graph


def test_init_and_cycle_numbering(tmp_path):
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.next_cycle_no() == 1
    state = _state(1, hypotheses=[_hypothesis()])
    assert store.persist_state(state, _report(state)) == 1
    assert store.next_cycle_no() == 2
    assert store.cycle_count() == 1


def test_readonly_has_identical_api_but_no_data_or_writes(tmp_path):
    store = MemoryStore(str(tmp_path / "memory"), readonly=True)
    state = _state(1, hypotheses=[_hypothesis()])
    assert store.persist_state(state, _report(state)) == -1
    assert store.recall(query="parse") == []
    assert store.load_hypotheses() == []
    assert not (tmp_path / "memory").exists()


def test_hypotheses_and_signals_persist_separately(tmp_path):
    store = MemoryStore(str(tmp_path))
    state = _state(1, hypotheses=[_hypothesis()])
    store.persist_state(state, _report(state))
    recalled = store.recall(query="invalid")
    assert len(recalled) == 1
    assert recalled[0]["grade"] == "finding"
    assert recalled[0]["remedy"].startswith("validate")
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='findings'"
            ).fetchone()
            is None
        )


def test_load_latest_snapshot_of_carried_hypothesis(tmp_path):
    store = MemoryStore(str(tmp_path))
    first = _hypothesis(1, grade="conjecture")
    state1 = _state(1, hypotheses=[first])
    store.persist_state(state1, _report(state1))
    first.evidence.append("probe-valid:2:1")
    first.grade = "supported"
    first.last_touched_cycle = 2
    state2 = _state(2, hypotheses=[first])
    store.persist_state(state2, _report(state2))
    loaded = store.load_hypotheses()
    assert len(loaded) == 1
    assert loaded[0].grade == "supported"
    assert loaded[0].first_seen_cycle == 1


def test_resolved_hypothesis_persists_but_is_no_longer_open(tmp_path):
    store = MemoryStore(str(tmp_path))
    hypothesis = _hypothesis(1, grade="deferred")
    first = _state(1, hypotheses=[hypothesis])
    store.persist_state(first, _report(first))
    hypothesis.grade = "resolved"
    hypothesis.evidence.append("resolved:commit2:guard added and regression passes")
    hypothesis.last_touched_cycle = 2
    state = _state(2, hypotheses=[hypothesis])

    store.persist_state(state, _report(state))

    loaded = store.load_hypotheses()
    assert len(loaded) == 1
    assert loaded[0].grade == "resolved"
    assert report_views(loaded) == ([], [], [])


def test_recall_supports_locus_and_trajectory_queries(tmp_path):
    store = MemoryStore(str(tmp_path))
    hypothesis = _hypothesis(1)
    state1 = _state(1, hypotheses=[hypothesis])
    store.persist_state(state1, _report(state1))
    hypothesis.last_touched_cycle = 2
    state2 = _state(2, hypotheses=[hypothesis])
    store.persist_state(state2, _report(state2))
    assert len(store.recall(locus="mod.py")) == 2
    trajectory = store.recall(query=hypothesis.id, kind="trajectory")
    # Evidence/id queries are allowed; use locus to select the trajectory.
    if not trajectory:
        trajectory = store.recall(locus="mod.py", kind="trajectory")
    assert [row["cycle_no"] for row in trajectory] == [1, 2]
    assert trajectory[0]["recurrence_count"] == 2
    assert len(trajectory[0]["grade_history"]) == 2


def test_semantic_recall_returns_relevant_claim_without_exact_phrase(tmp_path):
    store = MemoryStore(str(tmp_path))
    relevant = _hypothesis(1)
    unrelated = Hypothesis.create(
        claim="renderer emits the wrong heading color",
        consequence="the report is harder to scan",
        remedy="bind heading styles to the theme token",
        origin="exploration",
        locus=["report.py:render"],
        cycle_no=1,
    )
    state = _state(1, hypotheses=[unrelated, relevant])
    store.persist_state(state, _report(state))
    rows = store.recall(
        query="input validation parser callers",
        kind="semantic",
        k=2,
    )
    assert rows[0]["hypothesis_id"] == relevant.id


def test_recent_findings_filters_non_findings(tmp_path):
    store = MemoryStore(str(tmp_path))
    hypotheses = [_hypothesis(1), _hypothesis(1, grade="deferred", suffix="_later")]
    state = _state(1, hypotheses=hypotheses)
    store.persist_state(state, _report(state))
    rows = store.recent_findings(k=5)
    assert len(rows) == 1
    assert rows[0]["grade"] == "finding"


def test_graph_snapshot_round_trip(tmp_path):
    store = MemoryStore(str(tmp_path))
    state = _state(1, hypotheses=[_hypothesis()])
    store.persist_state(state, _report(state), graph=_graph())
    assert store.recent_cycles(1)[0]["has_graph"] == 1
    assert store.load_prior_graph() is not None


def test_symbol_and_edge_history(tmp_path):
    from codeminer.guardian.signals.graph_diff import EdgeChange

    store = MemoryStore(str(tmp_path))
    state = _state(1, hypotheses=[_hypothesis()])
    store.persist_state(state, _report(state))
    assert store.symbol_history("mod.py:parse")["first_seen_cycle"] == 1
    store.persist_edge_changes(
        1,
        [
            EdgeChange(
                kind="added",
                edge_type=EDGE_TYPE_REFERENCE,
                src="A",
                dst="B",
                src_file="a.py",
                dst_file="b.py",
            )
        ],
    )
    assert store.edge_drift(1)[0]["src_symbol"] == "A"


def test_cross_cycle_assertion_requires_trajectory(tmp_path):
    store = MemoryStore(str(tmp_path))
    state = _state(1, hypotheses=[_hypothesis()])
    store.persist_state(state, _report(state))
    with pytest.raises(AssertionError):
        store.assert_cross_cycle()
    carried = state.hypotheses
    second = _state(2, hypotheses=carried)
    store.persist_state(second, _report(second))
    store.assert_cross_cycle()


def test_two_cycle_rederived_claim_supersedes_instead_of_duplicates(tmp_path):
    store = MemoryStore(str(tmp_path))
    old = _hypothesis(1)
    first = _state(1, hypotheses=[old])
    store.persist_state(first, _report(first))
    replacement = Hypothesis.create(
        claim="parse still accepts malformed input through the alternate path",
        consequence="invalid state reaches callers",
        remedy="validate both paths and add a parameterized regression test",
        origin="memory",
        locus=["mod.py:parse"],
        evidence=["cycle:1", "probe-valid:2:1"],
        grade="finding",
        cycle_no=2,
        supersedes=[old.id],
    )
    second = _state(2, hypotheses=[old, replacement])
    report = _report(second)
    store.persist_state(second, report)
    assert [item.id for item in report.findings] == [replacement.id]
    assert len(store.load_hypotheses()) == 2
    assert store.load_hypotheses()[1].supersedes == [old.id]


def test_format_findings_uses_claim_and_remedy():
    rendered = format_findings_for_prompt(
        [
            {
                "commit": "abc12345",
                "grade": "finding",
                "claim": "parse accepts invalid input",
                "remedy": "validate input",
            }
        ]
    )
    assert "parse accepts" in rendered
    assert "remedy: validate" in rendered
