# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the host boundary around Guardian's stateful outer loop."""

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codeminer.guardian.cycle import GuardianConfig, _merge_llm_usage, run_cycle
from codeminer.guardian.investigator.runner import LLMUsage
from codeminer.guardian.memory import MemoryStore
from codeminer.guardian.report import render_markdown


class _FakeManifest:
    commit = "deadbeefcafebabe"
    file_count = 7
    capabilities = {"sparse_search": True}
    indexes = {}


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
    for index in range(3):
        (repo / "mod.py").write_text(
            f"def parse(value):\n    return {index}\n", encoding="utf-8"
        )
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", f"rev {index}")
    return repo


def _call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(*calls, tokens=5):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="", tool_calls=list(calls)))
        ],
        usage=SimpleNamespace(
            prompt_tokens=tokens,
            completion_tokens=0,
            total_tokens=tokens,
        ),
    )


class _ScriptedLLM:
    backend_name = "test"
    transport_history = ["test"]

    def __init__(self, responses):
        self.responses = list(responses)

    def _call_raw(self, messages, **kwargs):
        return self.responses.pop(0)


def _finding_script(signal_id):
    claim = "mod.parse accepts invalid input without validation"
    # The synthetic probe ref exercises the grade floor independently of L3;
    # investigation behavior is covered in test_outer_loop.py.
    return (
        _ScriptedLLM(
            [
                _response(_call("s1", "list_signals", {})),
                _response(
                    _call(
                        "h1",
                        "write_hypothesis",
                        {
                            "claim": claim,
                            "consequence": "invalid state reaches callers",
                            "remedy": "validate input and add a regression test",
                            "origin": "signal",
                            "locus": ["mod.py:parse"],
                            "evidence": [
                                f"signal:{signal_id}",
                                "probe-valid:fixture:1",
                            ],
                            "confidence": 0.8,
                        },
                    )
                ),
                # The stable id is derived from claim+locus; obtain it in a setup
                # call below rather than duplicating hashing logic here.
            ]
        ),
        claim,
    )


def test_no_model_is_explicitly_degraded_and_signals_do_not_become_findings(tmp_path):
    repo = _make_repo(tmp_path)
    report = run_cycle(
        GuardianConfig(
            repo_path=str(repo),
            since="10 years ago",
            checkpoint_path=str(tmp_path / "cycle.json"),
        ),
        manifest=_FakeManifest(),
    )
    assert report.commit == "deadbeefcafebabe"
    assert report.findings == []
    assert report.degraded is True
    assert report.analysis_status == "degraded"
    assert report.exit_reason == "Degraded"
    assert "No verified actionable findings" in render_markdown(report)


def test_merged_usage_total_is_the_sum_of_rendered_components():
    outer = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=12,
    )
    inner = SimpleNamespace(
        prompt_tokens=13,
        completion_tokens=5,
        total_tokens=14,
    )
    merged = LLMUsage()

    _merge_llm_usage(merged, outer, inner)

    assert merged.prompt_tokens == 24
    assert merged.completion_tokens == 12
    assert merged.total_tokens == 36


def test_agent_cycle_projects_only_graded_finding(tmp_path):
    from codeminer.guardian.loop import Hypothesis, Signal

    repo = _make_repo(tmp_path)
    signal = Signal.create(
        kind="churn",
        locus=["mod.py"],
        detail="mod.py changed in 3 commits over 10 years ago",
        value={"commit_count": 3, "window": "10 years ago"},
    )
    scripted, claim = _finding_script(signal.id)
    hypothesis_id = Hypothesis.create(
        claim=claim,
        consequence="invalid state reaches callers",
        remedy="validate input and add a regression test",
        origin="signal",
        locus=["mod.py:parse"],
    ).id
    scripted.responses.extend(
        [
            _response(
                _call(
                    "u1",
                    "update_hypothesis",
                    {
                        "id": hypothesis_id,
                        "grade": "finding",
                        "reason": "the probe verified the contract violation",
                    },
                )
            ),
            _response(_call("r1", "submit_report", {"summary": "One finding."})),
        ]
    )
    with patch("codeminer.guardian.cycle._make_llm", return_value=scripted):
        report = run_cycle(
            GuardianConfig(
                repo_path=str(repo),
                since="10 years ago",
                use_llm=True,
                checkpoint_path=str(tmp_path / "cycle.json"),
            ),
            manifest=_FakeManifest(),
        )
    assert report.exit_reason == "ReportSubmitted"
    assert len(report.findings) == 1
    assert report.findings[0].claim == claim
    assert report.findings[0].remedy.startswith("validate")
    assert (
        len([row for row in report.decision_log if row.get("event") == "tool_call"])
        == 4
    )


def test_memoryless_recall_tool_is_present_and_returns_empty(tmp_path):
    repo = _make_repo(tmp_path)
    llm = _ScriptedLLM(
        [
            _response(
                _call(
                    "m1",
                    "recall",
                    {"query": "parse", "kind": "recent", "k": 5},
                )
            ),
            _response(_call("r1", "submit_report", {"summary": "No memory."})),
        ]
    )
    with patch("codeminer.guardian.cycle._make_llm", return_value=llm):
        report = run_cycle(
            GuardianConfig(
                repo_path=str(repo),
                use_llm=True,
                arm="memoryless",
                memory_dir=str(tmp_path / "memory"),
                checkpoint_path=str(tmp_path / "cycle.json"),
            ),
            manifest=_FakeManifest(),
        )
    recall = next(row for row in report.decision_log if row.get("tool") == "recall")
    assert recall["observation"] == "[]"
    assert report.exit_reason == "ReportSubmitted"
    assert not (tmp_path / "memory").exists()


def test_memory_cycle_persists_hypothesis_trajectory(tmp_path):
    from codeminer.guardian.loop import Hypothesis, Signal

    repo = _make_repo(tmp_path)
    signal = Signal.create(
        kind="churn",
        locus=["mod.py"],
        detail="mod.py changed in 3 commits over 10 years ago",
        value={"commit_count": 3, "window": "10 years ago"},
    )
    scripted, claim = _finding_script(signal.id)
    hypothesis_id = Hypothesis.create(
        claim=claim,
        consequence="invalid state reaches callers",
        remedy="validate input and add a regression test",
        origin="signal",
        locus=["mod.py:parse"],
    ).id
    scripted.responses.extend(
        [
            _response(
                _call(
                    "u1",
                    "update_hypothesis",
                    {
                        "id": hypothesis_id,
                        "grade": "finding",
                        "reason": "verified",
                    },
                )
            ),
            _response(_call("r1", "submit_report", {"summary": "done"})),
        ]
    )
    memory_dir = tmp_path / "memory"
    with patch("codeminer.guardian.cycle._make_llm", return_value=scripted):
        run_cycle(
            GuardianConfig(
                repo_path=str(repo),
                since="10 years ago",
                use_llm=True,
                memory_dir=str(memory_dir),
                checkpoint_path=str(tmp_path / "cycle.json"),
                graph_snapshot=False,
            ),
            manifest=_FakeManifest(),
        )
    store = MemoryStore(str(memory_dir))
    assert store.cycle_count() == 1
    assert store.recent_findings(1)[0]["claim"] == claim


def test_exploration_finding_without_signal_evidence_is_measured(tmp_path):
    from codeminer.guardian.loop import Hypothesis

    repo = _make_repo(tmp_path)
    claim = "mod.parse and its caller disagree on the invalid-input contract"
    hypothesis_id = Hypothesis.create(
        claim=claim,
        consequence="the caller handles an impossible state",
        remedy="align the contracts and add a cross-function regression test",
        origin="exploration",
        locus=["mod.py:parse", "mod.py:caller"],
    ).id
    llm = _ScriptedLLM(
        [
            _response(
                _call(
                    "h1",
                    "write_hypothesis",
                    {
                        "claim": claim,
                        "consequence": "the caller handles an impossible state",
                        "remedy": (
                            "align the contracts and add a cross-function "
                            "regression test"
                        ),
                        "origin": "exploration",
                        "locus": ["mod.py:parse", "mod.py:caller"],
                        "evidence": ["probe-valid:fixture:exploration"],
                        "confidence": 0.7,
                    },
                )
            ),
            _response(
                _call(
                    "u1",
                    "update_hypothesis",
                    {
                        "id": hypothesis_id,
                        "grade": "finding",
                        "reason": "probe verified the mismatch",
                    },
                )
            ),
            _response(_call("r1", "submit_report", {"summary": "proactive"})),
        ]
    )
    with patch("codeminer.guardian.cycle._make_llm", return_value=llm):
        report = run_cycle(
            GuardianConfig(
                repo_path=str(repo),
                use_llm=True,
                checkpoint_path=str(tmp_path / "cycle.json"),
            ),
            manifest=_FakeManifest(),
        )
    assert report.findings[0].origin == "exploration"
    assert report.trace_metrics["proactive_findings"] == 1
    assert report.trace_metrics["no_signal_hypothesis_fraction"] == 1.0


def test_zero_budget_is_typed_exit(tmp_path):
    repo = _make_repo(tmp_path)
    with patch(
        "codeminer.guardian.cycle._make_llm",
        return_value=_ScriptedLLM([]),
    ):
        report = run_cycle(
            GuardianConfig(
                repo_path=str(repo),
                use_llm=True,
                budget_tokens=0,
                checkpoint_path=str(tmp_path / "cycle.json"),
            ),
            manifest=_FakeManifest(),
        )
    assert report.exit_reason == "BudgetExceeded"
    assert report.findings == []


@pytest.mark.integration
def test_run_cycle_end_to_end_bm25_only_is_non_modifying(tmp_path):
    repo = _make_repo(tmp_path)
    report = run_cycle(
        GuardianConfig(
            repo_path=str(repo),
            since="10 years ago",
            checkpoint_path=str(tmp_path / "cycle.json"),
        )
    )
    assert report.commit
    assert report.degraded
    markdown = render_markdown(report)
    assert "Repository Guardian Report" in markdown
    assert "diff --git" not in markdown
