# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Codex filesystem bridge."""

import json
from unittest.mock import Mock

from codeminer.guardian.cycle import GuardianConfig
from codeminer.guardian.loop import Hypothesis
from codeminer.guardian.report import GuardianReport, report_views
from deepsweguardian import codex_bridge


def _report() -> GuardianReport:
    hypothesis = Hypothesis.create(
        claim="pkg.mod violates its dependency contract",
        consequence="dependent callers may fail",
        remedy="restore the contract and add regression coverage",
        origin="exploration",
        locus=["pkg/mod.py"],
        evidence=["probe-valid:fixture:1"],
        grade="finding",
    )
    findings, backlog, retractions = report_views([hypothesis])
    return GuardianReport(
        repo="/repo",
        commit="abc123def456",
        generated_at="2026-07-20 00:00:00 UTC",
        churn_window="90 days ago",
        llm_model="codex:gpt-5.6-luna",
        llm_backend="codex-sdk",
        llm_transport_history=["codex-sdk"],
        findings=findings,
        backlog=backlog,
        retractions=retractions,
    )


def test_run_bridge_once_writes_markdown_json_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_bridge, "_head", lambda _repo: "abc123def456")
    monkeypatch.setattr(codex_bridge, "run_cycle", lambda _cfg: _report())

    cfg = GuardianConfig(repo_path="/repo", use_llm=True)
    codex_bridge.run_bridge(
        cfg,
        out_dir=str(tmp_path),
        poll_interval=1,
        once=True,
        baseline_commit="base000",
    )

    md = (tmp_path / "findings.md").read_text(encoding="utf-8")
    data = json.loads((tmp_path / "findings.json").read_text(encoding="utf-8"))
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))

    assert "pkg.mod violates its dependency contract" in md
    assert data["findings"][0]["remedy"].startswith("restore")
    assert data["llm_model"] == "codex:gpt-5.6-luna"
    assert data["llm_backend"] == "codex-sdk"
    assert status["commit"] == "abc123def456"
    assert status["findings"] == 1
    assert status["backlog"] == 0
    assert status["degraded"] is False
    assert status["analysis_status"] == "complete"
    assert status["llm_model"] == "codex:gpt-5.6-luna"
    assert status["llm_backend"] == "codex-sdk"
    assert status["llm_transport_history"] == ["codex-sdk"]
    assert status["running"] is False


def test_run_bridge_once_does_not_analyze_the_baseline(tmp_path, monkeypatch):
    run_cycle = Mock()
    monkeypatch.setattr(codex_bridge, "_head", lambda _repo: "base000")
    monkeypatch.setattr(codex_bridge, "run_cycle", run_cycle)

    cfg = GuardianConfig(repo_path="/repo", use_llm=True)
    codex_bridge.run_bridge(
        cfg,
        out_dir=str(tmp_path),
        poll_interval=1,
        once=True,
        baseline_commit="base000",
    )

    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    run_cycle.assert_not_called()
    assert not (tmp_path / "findings.json").exists()
    assert status["commit"] == "base000"
    assert status["llm_backend"] == "not_started"
    assert status["running"] is False


def test_main_builds_llm_enabled_guardian_config(tmp_path, monkeypatch):
    seen = {}

    def fake_run_bridge(
        config, *, out_dir, poll_interval, once=False, baseline_commit=None
    ):
        seen["config"] = config
        seen["out_dir"] = out_dir
        seen["poll_interval"] = poll_interval
        seen["once"] = once
        seen["baseline_commit"] = baseline_commit

    monkeypatch.setattr(codex_bridge, "run_bridge", fake_run_bridge)

    codex_bridge.main(
        [
            "--repo",
            "/repo",
            "--out-dir",
            str(tmp_path),
            "--arm",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--model",
            "vertex_ai/gemini-2.5-flash",
            "--top-n",
            "3",
            "--budget-tokens",
            "1234",
            "--poll-interval",
            "2",
            "--once",
        ]
    )

    cfg = seen["config"]
    assert cfg.use_llm is True
    assert cfg.llm_model == "vertex_ai/gemini-2.5-flash"
    assert cfg.top_n == 3
    assert cfg.budget_tokens == 1234
    assert seen["out_dir"] == str(tmp_path)
    assert seen["poll_interval"] == 2
    assert seen["once"] is True
    assert seen["baseline_commit"] is None


def test_main_can_disable_cycle_token_limit(tmp_path, monkeypatch):
    seen = {}

    def fake_run_bridge(
        config, *, out_dir, poll_interval, once=False, baseline_commit=None
    ):
        seen["config"] = config

    monkeypatch.setattr(codex_bridge, "run_bridge", fake_run_bridge)

    codex_bridge.main(
        [
            "--repo",
            "/repo",
            "--out-dir",
            str(tmp_path),
            "--no-budget-limit",
            "--once",
        ]
    )

    assert seen["config"].budget_tokens is None


def test_main_defaults_out_dir_to_home_guardian(monkeypatch):
    seen = {}

    def fake_run_bridge(
        config, *, out_dir, poll_interval, once=False, baseline_commit=None
    ):
        seen["out_dir"] = out_dir

    monkeypatch.setattr(codex_bridge, "run_bridge", fake_run_bridge)
    monkeypatch.setenv("HOME", "/tmp/codex-home")

    codex_bridge.main(["--repo", "/repo", "--once"])

    assert seen["out_dir"] == "/tmp/codex-home/.guardian"


def test_main_reads_recorded_baseline_file(tmp_path, monkeypatch):
    seen = {}
    baseline_file = tmp_path / "base_commit"
    baseline_file.write_text("base000\n", encoding="utf-8")

    def fake_run_bridge(
        config, *, out_dir, poll_interval, once=False, baseline_commit=None
    ):
        seen["baseline_commit"] = baseline_commit

    monkeypatch.setattr(codex_bridge, "run_bridge", fake_run_bridge)

    codex_bridge.main(
        [
            "--repo",
            "/repo",
            "--out-dir",
            str(tmp_path),
            "--baseline-file",
            str(baseline_file),
            "--once",
        ]
    )

    assert seen["baseline_commit"] == "base000"
