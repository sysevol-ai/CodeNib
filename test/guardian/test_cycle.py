# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for codeminer.guardian.cycle.run_cycle.

The orchestration test injects a fake manifest + investigator (unit tier). The
end-to-end test drives the real IndexCompiler with BM25 only (integration tier).
"""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from codeminer.guardian.cycle import GuardianConfig, run_cycle
from codeminer.guardian.report import render_markdown


class _FakeManifest:
    commit = "deadbeefcafebabe"
    file_count = 7
    capabilities = {"sparse_search": True}


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    for i in range(3):
        (repo / "mod.py").write_text(f"def f():\n    return {i}\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", f"rev {i}")
    return repo


def test_run_cycle_with_injected_manifest(tmp_path):
    repo = _make_repo(tmp_path)

    config = GuardianConfig(repo_path=str(repo), since="10 years ago")
    report = run_cycle(config, manifest=_FakeManifest())

    assert report.commit == "deadbeefcafebabe"  # from injected manifest
    assert report.file_count == 7
    assert len(report.findings) == 1
    assert report.findings[0].kind == "churn"
    # _NullRetriever returns no results → evidence list is empty (investigate step ran)

    md = render_markdown(report)
    assert "mod.py" in md and "non-modifying" in md.lower()


def test_run_cycle_injected_reporter_narrative_flows_through(tmp_path):
    """Injected reporter's narrative appears in Finding and rendered Markdown."""
    repo = _make_repo(tmp_path)

    def fake_reporter(hotspot):
        return f"LLM says {hotspot.path} is risky."

    config = GuardianConfig(repo_path=str(repo), since="10 years ago")
    report = run_cycle(config, reporter=fake_reporter, manifest=_FakeManifest())

    assert report.findings[0].narrative == "LLM says mod.py is risky."
    md = render_markdown(report)
    assert "LLM Analysis" in md
    assert "LLM says mod.py is risky." in md


@pytest.mark.slow
def test_run_cycle_use_llm_flag_calls_litellm(tmp_path):
    """use_llm=True wires up the LLM reporter; narrative ends up in the report."""
    repo = _make_repo(tmp_path)

    def _make_tool_call(call_id, query):
        tc = MagicMock()
        tc.id = call_id
        tc.function.name = "search_code"
        tc.function.arguments = json.dumps({"query": query})
        return tc

    def _resp(content, tool_calls=None):
        msg = MagicMock()
        msg.content = content
        msg.tool_calls = tool_calls or []
        choice = MagicMock()
        choice.message = msg
        r = MagicMock()
        r.choices = [choice]
        return r

    tc = _make_tool_call("c1", "mod f function")
    side_effects = [
        _resp("", tool_calls=[tc]),  # first call: one tool round
        _resp("mod.py changes frequently and risks instability."),  # final answer
    ]

    config = GuardianConfig(
        repo_path=str(repo),
        since="10 years ago",
        use_llm=True,
        llm_max_tool_rounds=1,
    )

    with patch(
        "codeminer.guardian.llm_investigator._read_hotspot_file",
        return_value="def f():\n    return 0\n",
    ), patch("litellm.completion", side_effect=side_effects):
        report = run_cycle(config, manifest=_FakeManifest())

    assert report.findings
    assert "instability" in report.findings[0].narrative
    md = render_markdown(report)
    assert "LLM Analysis" in md
    assert "instability" in md


@pytest.mark.slow
def test_run_cycle_test_failure_llm_narrative(tmp_path):
    """When use_llm=True and a test fails, the Finding gets an LLM narrative."""
    repo = _make_repo(tmp_path)

    def _resp(content, tool_calls=None):
        msg = MagicMock()
        msg.content = content
        msg.tool_calls = tool_calls or []
        choice = MagicMock()
        choice.message = msg
        r = MagicMock()
        r.choices = [choice]
        return r

    side_effects = [_resp("The test fails because run() returns the wrong value.")]

    config = GuardianConfig(
        repo_path=str(repo),
        since="10 years ago",
        run_tests=True,
        use_llm=True,
        llm_max_tool_rounds=0,
    )

    # Inject a fake test result with one failure so we don't shell out to pytest.
    from codeminer.guardian.signals import TestFailure, TestResult

    fake_result = TestResult(
        ran=True,
        passed=0,
        failed=1,
        failures=[TestFailure(nodeid="test/mod.py::test_run", message="AssertionError")],
        summary="1 failed",
    )

    with patch(
        "codeminer.guardian.llm_investigator._read_hotspot_file", return_value=""
    ), patch(
        "codeminer.guardian.llm_investigator.read_file", return_value="def test_run(): pass\n"
    ), patch(
        "codeminer.guardian.cycle.run_test_suite", return_value=fake_result
    ), patch(
        "litellm.completion", side_effect=side_effects
    ):
        report = run_cycle(config, manifest=_FakeManifest())

    test_findings = [f for f in report.findings if f.kind == "test_failure"]
    assert test_findings
    assert "wrong value" in test_findings[0].narrative
    assert "LLM Analysis" in render_markdown(report)


class TestCycleMemoryIntegration:
    """Tests for cross-cycle memory wiring in run_cycle."""

    def test_memory_dir_creates_store_and_persists(self, tmp_path):
        """With memory_dir set, run_cycle writes a cycle row to the SQLite store."""
        repo = _make_repo(tmp_path)
        memory_dir = str(tmp_path / "memory")

        config = GuardianConfig(
            repo_path=str(repo),
            since="10 years ago",
            memory_dir=memory_dir,
            graph_snapshot=False,  # no symbol_graph artifact in FakeManifest
        )
        run_cycle(config, manifest=_FakeManifest())

        from codeminer.guardian.memory import MemoryStore
        store = MemoryStore(memory_dir)
        assert store.cycle_count() == 1
        findings = store.recent_findings(k=10)
        assert len(findings) >= 1
        assert any("mod.py" in f["title"] for f in findings)

    def test_two_cycles_satisfy_cross_cycle_assertion(self, tmp_path):
        """Two consecutive cycles with memory_dir satisfy the M2 assertion."""
        repo = _make_repo(tmp_path)
        memory_dir = str(tmp_path / "memory")

        config = GuardianConfig(
            repo_path=str(repo),
            since="10 years ago",
            memory_dir=memory_dir,
            graph_snapshot=False,
        )
        run_cycle(config, manifest=_FakeManifest())
        run_cycle(config, manifest=_FakeManifest())

        from codeminer.guardian.memory import MemoryStore
        store = MemoryStore(memory_dir)
        store.assert_cross_cycle()  # must not raise

    def test_memoryless_arm_does_not_persist(self, tmp_path):
        """arm='memoryless' runs the same code path but writes nothing to the store."""
        repo = _make_repo(tmp_path)
        memory_dir = str(tmp_path / "memory")

        config = GuardianConfig(
            repo_path=str(repo),
            since="10 years ago",
            memory_dir=memory_dir,
            arm="memoryless",
            graph_snapshot=False,
        )
        run_cycle(config, manifest=_FakeManifest())

        from codeminer.guardian.memory import MemoryStore
        store = MemoryStore(memory_dir)
        assert store.cycle_count() == 0

    def test_no_memory_dir_report_unchanged(self, tmp_path):
        """Without memory_dir, run_cycle behaves identically to Phase 1."""
        repo = _make_repo(tmp_path)
        config = GuardianConfig(repo_path=str(repo), since="10 years ago")
        report = run_cycle(config, manifest=_FakeManifest())
        # Only churn findings — no drift findings without graph diff
        assert all(f.kind == "churn" for f in report.findings)

    def test_drift_findings_appended_when_graph_diff_runs(self, tmp_path):
        """When a prior graph and current graph both exist, drift findings are added."""
        from unittest.mock import patch as _patch

        from codeminer.graph.code_graph import CodeGraph
        from codeminer.guardian.graph_diff import DriftSignal
        from codeminer.types import EDGE_TYPE_REFERENCE

        repo = _make_repo(tmp_path)
        memory_dir = str(tmp_path / "memory")

        # Build a minimal prior graph and save it into the memory store.
        prior = CodeGraph()
        prior._add_vertex("A", {"type": "function", "file": "a.py"})
        prior._add_vertex("B", {"type": "function", "file": "b.py"})
        prior._add_edge("A", "B", EDGE_TYPE_REFERENCE)

        from codeminer.guardian.memory import MemoryStore
        store = MemoryStore(memory_dir)
        # Manually persist a fake prior cycle with a graph.
        from codeminer.guardian.report import Finding, GuardianReport
        fake_prior_report = GuardianReport(
            repo=str(repo),
            commit="prior000",
            generated_at="2026-01-01 00:00:00 UTC",
            churn_window="90 days ago",
        )
        store.persist_cycle(fake_prior_report, graph=prior, memory_dir=memory_dir)

        # Build a "current" graph that has an extra edge — triggers drift signal.
        current = CodeGraph()
        current._add_vertex("A", {"type": "function", "file": "a.py"})
        current._add_vertex("B", {"type": "function", "file": "b.py"})
        current._add_edge("A", "B", EDGE_TYPE_REFERENCE)
        for i in range(5):
            current._add_vertex(f"C{i}", {"type": "function", "file": f"c{i}.py"})
            current._add_edge(f"C{i}", "B", EDGE_TYPE_REFERENCE)  # fan-in spike on B

        # Patch _load_current_graph to return our synthetic current graph.
        with _patch(
            "codeminer.guardian.cycle._load_current_graph",
            return_value=current,
        ):
            config = GuardianConfig(
                repo_path=str(repo),
                since="10 years ago",
                memory_dir=memory_dir,
                graph_snapshot=False,  # skip snapshot write in this test
            )
            report = run_cycle(config, manifest=_FakeManifest())

        drift = [f for f in report.findings if f.kind == "drift"]
        assert len(drift) >= 1
        assert any("fan_in_spike" in f.title for f in drift)


@pytest.mark.integration
def test_run_cycle_end_to_end_bm25_only(tmp_path):
    """One real cycle: compile a BM25 index (no embeddings) and render a report."""
    repo = _make_repo(tmp_path)
    config = GuardianConfig(
        repo_path=str(repo),
        since="10 years ago",
        index_types=("bm25",),
    )
    report = run_cycle(config)

    assert report.commit  # real HEAD from compile/git
    assert report.findings  # mod.py is a hotspot
    md = render_markdown(report)
    assert "Repository Guardian Report" in md
    assert "diff --git" not in md  # non-modifying invariant
