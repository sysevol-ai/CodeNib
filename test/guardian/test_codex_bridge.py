# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Codex filesystem bridge."""

import json

from codeminer.guardian import codex_bridge
from codeminer.guardian.cycle import GuardianConfig
from codeminer.guardian.report import Finding, GuardianReport


def _report() -> GuardianReport:
    return GuardianReport(
        repo="/repo",
        commit="abc123def456",
        generated_at="2026-07-20 00:00:00 UTC",
        churn_window="90 days ago",
        findings=[
            Finding(
                kind="churn",
                title="High-churn file: pkg/mod.py",
                detail="Changed in **5** commits.",
                narrative="Guardian found a risky dependency contract.",
                verdict="confirmed",
            )
        ],
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
    )

    md = (tmp_path / "findings.md").read_text(encoding="utf-8")
    data = json.loads((tmp_path / "findings.json").read_text(encoding="utf-8"))
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))

    assert "High-churn file: pkg/mod.py" in md
    assert data["findings"][0]["verdict"] == "confirmed"
    assert status["commit"] == "abc123def456"
    assert status["findings"] == 1
    assert status["running"] is False


def test_main_builds_llm_enabled_guardian_config(tmp_path, monkeypatch):
    seen = {}

    def fake_run_bridge(config, *, out_dir, poll_interval, once=False):
        seen["config"] = config
        seen["out_dir"] = out_dir
        seen["poll_interval"] = poll_interval
        seen["once"] = once

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


def test_main_defaults_out_dir_to_home_guardian(monkeypatch):
    seen = {}

    def fake_run_bridge(config, *, out_dir, poll_interval, once=False):
        seen["out_dir"] = out_dir

    monkeypatch.setattr(codex_bridge, "run_bridge", fake_run_bridge)
    monkeypatch.setenv("HOME", "/tmp/codex-home")

    codex_bridge.main(["--repo", "/repo", "--once"])

    assert seen["out_dir"] == "/tmp/codex-home/.guardian"
