# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Codex filesystem bridge."""

import json

from codenib.clients.execution import (
    AgentRunResult,
    ExecutionIsolation,
    RunStatus,
    TokenUsage,
)
from codenib.clients.guardian import (
    Evidence,
    FindingStatus,
    GuardianConfig,
    GuardianFinding,
    GuardianResult,
    ReviewStatus,
)
from scripts.guardian.deepswe.harness import bridge as codex_bridge


def _result() -> GuardianResult:
    finding = GuardianFinding(
        statement="Public state copies preserve the selected mode",
        status=FindingStatus.VIOLATED,
        evidence=(
            Evidence(
                path="tests/test_state.py",
                line_start=12,
                line_end=16,
                description="the public copy path must retain mode",
            ),
        ),
        patch_assessment="the candidate copy omits mode",
        recommendation="preserve mode in every public copy path",
        confidence=0.9,
    )
    rollout = AgentRunResult(
        status=RunStatus.COMPLETED,
        final_message="{}",
        trajectory=(),
        usage=TokenUsage(input_tokens=100, cached_input_tokens=60, output_tokens=10),
        duration_seconds=1,
        raw_output='{"type":"turn.completed"}\n',
        stderr="",
        exit_code=0,
    )
    return GuardianResult(
        base_commit="a" * 40,
        candidate_commit="b" * 40,
        status=ReviewStatus.COMPLETE,
        findings=(finding,),
        summary="One mismatch remains.",
        rollouts=(rollout,),
    )


class FakeGuardian:
    def __init__(self, result):
        self.result = result
        self.requests = []

    async def review(self, request):
        self.requests.append(request)
        return self.result


def test_run_bridge_once_writes_report_status_and_rollouts(tmp_path, monkeypatch):
    result = _result()
    reviewer = FakeGuardian(result)
    heads = iter([result.candidate_commit])
    monkeypatch.setattr(codex_bridge, "_head", lambda _repo: next(heads))
    monkeypatch.setattr(codex_bridge, "GuardianAgent", lambda *_a, **_k: reviewer)
    monkeypatch.setenv("GUARDIAN_EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("GUARDIAN_RUNTIME_PYTHON", "/opt/codenib-env/bin/python")
    inbox = tmp_path / "messages.jsonl"

    codex_bridge.run_bridge(
        GuardianConfig(explorer_model="cheap", aggregator_model="strong"),
        repo_path=str(tmp_path),
        message_inbox=str(inbox),
        out_dir=str(tmp_path / "out"),
        poll_interval=1,
        once=True,
        baseline_commit=result.base_commit,
    )

    output = tmp_path / "out"
    markdown = (output / "findings.md").read_text(encoding="utf-8")
    data = json.loads((output / "findings.json").read_text(encoding="utf-8"))
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert "Public state copies preserve" in markdown
    assert data["findings"][0]["status"] == "violated"
    assert status["commit"] == result.candidate_commit
    assert status["analysis_status"] == "complete"
    assert status["exit_reason"] == "ReviewCompleted"
    assert status["llm_backend"] == "codex-cli"
    assert status["llm_tokens"]["total"] == 110
    assert status["interpreters"]["guardian_runtime_python"] == (
        "/opt/codenib-env/bin/python"
    )
    assert (output / "rollouts" / "rollout_01.jsonl").exists()
    assert reviewer.requests[0].base_commit == result.base_commit


def test_run_bridge_does_not_review_baseline(tmp_path, monkeypatch):
    result = _result()
    reviewer = FakeGuardian(result)
    monkeypatch.setattr(codex_bridge, "_head", lambda _repo: result.base_commit)
    monkeypatch.setattr(codex_bridge, "GuardianAgent", lambda *_a, **_k: reviewer)

    codex_bridge.run_bridge(
        GuardianConfig(explorer_model="cheap", aggregator_model="strong"),
        repo_path=str(tmp_path),
        message_inbox=str(tmp_path / "messages.jsonl"),
        out_dir=str(tmp_path / "out"),
        poll_interval=1,
        once=True,
        baseline_commit=result.base_commit,
    )

    assert reviewer.requests == []
    status = json.loads((tmp_path / "out" / "status.json").read_text())
    assert status["analysis_status"] == "pending"
    assert status["llm_backend"] == "not_started"


def test_main_builds_reframed_guardian_config(tmp_path, monkeypatch):
    seen = {}

    def fake_run_bridge(config, **kwargs):
        seen["config"] = config
        seen.update(kwargs)

    monkeypatch.setattr(codex_bridge, "run_bridge", fake_run_bridge)
    codex_bridge.main(
        [
            "--repo",
            str(tmp_path),
            "--out-dir",
            str(tmp_path / "out"),
            "--message-inbox",
            str(tmp_path / "messages.jsonl"),
            "--explorer-model",
            "codex:gpt-cheap",
            "--aggregator-model",
            "codex:gpt-strong",
            "--explorer-count",
            "3",
            "--max-findings",
            "4",
            "--rollout-timeout",
            "123",
            "--once",
        ]
    )

    config = seen["config"]
    assert config.explorer_model == "gpt-cheap"
    assert config.aggregator_model == "gpt-strong"
    assert config.explorer_count == 3
    assert config.max_findings == 4
    assert config.rollout_timeout_seconds == 123
    assert config.execution_isolation is ExecutionIsolation.EXTERNAL
    assert seen["repo_path"] == str(tmp_path)
    assert seen["once"] is True


def test_main_reads_recorded_baseline_file(tmp_path, monkeypatch):
    baseline = tmp_path / "base_commit"
    baseline.write_text("a" * 40 + "\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(
        codex_bridge,
        "run_bridge",
        lambda _config, **kwargs: seen.update(kwargs),
    )

    codex_bridge.main(
        [
            "--repo",
            str(tmp_path),
            "--baseline-file",
            str(baseline),
            "--once",
        ]
    )

    assert seen["baseline_commit"] == "a" * 40
