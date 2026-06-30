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
from codeminer.guardian.investigate import Evidence
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


def test_run_cycle_with_injected_manifest_and_investigator(tmp_path):
    repo = _make_repo(tmp_path)
    captured = []

    def fake_investigator(hotspot):
        captured.append(hotspot.path)
        return [Evidence("mod.py", "f", "function", 1, 2, 0.5)]

    config = GuardianConfig(repo_path=str(repo), since="10 years ago", investigate=True)
    report = run_cycle(config, investigator=fake_investigator, manifest=_FakeManifest())

    assert report.commit == "deadbeefcafebabe"  # from injected manifest
    assert report.file_count == 7
    assert captured == ["mod.py"]  # investigator was called per hotspot
    assert len(report.findings) == 1
    assert report.findings[0].kind == "churn"
    assert report.findings[0].evidence[0].node_name == "f"

    md = render_markdown(report)
    assert "mod.py" in md and "non-modifying" in md.lower()


def test_run_cycle_no_investigate_skips_evidence(tmp_path):
    repo = _make_repo(tmp_path)
    config = GuardianConfig(
        repo_path=str(repo), since="10 years ago", investigate=False
    )
    report = run_cycle(config, manifest=_FakeManifest())
    assert report.findings  # still surfaces churn
    assert all(not f.evidence for f in report.findings)


def test_run_cycle_injected_reporter_narrative_flows_through(tmp_path):
    """Injected reporter's narrative appears in Finding and rendered Markdown."""
    repo = _make_repo(tmp_path)

    def fake_investigator(hotspot):
        return [Evidence("mod.py", "f", "function", 1, 2, 0.5)]

    def fake_reporter(hotspot, evidence):
        return f"LLM says {hotspot.path} is risky."

    config = GuardianConfig(repo_path=str(repo), since="10 years ago")
    report = run_cycle(
        config,
        investigator=fake_investigator,
        reporter=fake_reporter,
        manifest=_FakeManifest(),
    )

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
        investigate=False,
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
        investigate=False,
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


@pytest.mark.integration
def test_run_cycle_end_to_end_bm25_only(tmp_path):
    """One real cycle: compile a BM25 index (no embeddings) and render a report."""
    repo = _make_repo(tmp_path)
    config = GuardianConfig(
        repo_path=str(repo),
        since="10 years ago",
        index_types=("bm25",),
        investigate=False,  # avoid embedding-model download in CI
    )
    report = run_cycle(config)

    assert report.commit  # real HEAD from compile/git
    assert report.findings  # mod.py is a hotspot
    md = render_markdown(report)
    assert "Repository Guardian Report" in md
    assert "diff --git" not in md  # non-modifying invariant
