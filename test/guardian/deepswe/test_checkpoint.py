# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Guardian final checkpoint script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.guardian.deepswe.harness.checkpoint import guardian_checkpoint_script


def test_solver_prompt_requires_one_foreground_checkpoint_call() -> None:
    prompt = (
        Path(__file__).parents[3]
        / "scripts/guardian/deepswe/harness/prompts/codex_file_bridge.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.lower().split())

    assert "one standalone foreground shell call" in prompt
    assert "Do not background" in prompt
    assert "parallel `sleep`, `pgrep`" in prompt
    assert "wait for that same shell execution to complete" in normalized
    assert "do not call the generic collaboration `wait` tool" in normalized


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "checkpoint@example.com")
    _git(repo, "config", "user.name", "Checkpoint")
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_script(tmp_path, *, start_command="", baseline_file="", exchange_dir=""):
    path = tmp_path / "guardian-checkpoint"
    path.write_text(
        guardian_checkpoint_script(
            start_command=start_command,
            baseline_file=baseline_file,
            exchange_dir=exchange_dir,
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_guardian_checkpoint_prints_fresh_report(tmp_path):
    repo, head = _init_repo(tmp_path)
    guardian_dir = tmp_path / "guardian"
    guardian_dir.mkdir()
    (guardian_dir / "status.json").write_text(
        json.dumps(
            {
                "commit": head,
                "running": False,
                "findings": 1,
                "uncertain_specifications": 1,
                "degraded": True,
                "analysis_status": "degraded",
                "exit_reason": "ReportSubmitted",
                "llm_model": "codex:gpt-5.6-luna",
                "explorer_model": "gpt-5.6-luna",
                "aggregator_model": "gpt-5.6-sol",
                "llm_backend": "codex-sdk",
                "llm_tokens": {"total": 123},
                "analysis_warnings": [
                    "explorer_1 candidate evidence path does not resolve"
                ],
                "error": "",
            }
        ),
        encoding="utf-8",
    )
    (guardian_dir / "findings.md").write_text(
        "# Findings\n\nRisk found.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_write_script(tmp_path)),
            "--repo",
            str(repo),
            "--guardian-dir",
            str(guardian_dir),
            "--timeout",
            "1",
            "--interval",
            "0.1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Guardian checkpoint: report ready" in result.stdout
    assert "explorer model: gpt-5.6-luna" in result.stdout
    assert "aggregator model: gpt-5.6-sol" in result.stdout
    assert "backend: codex-sdk" in result.stdout
    assert "uncertain specifications: 1" in result.stdout
    assert "confidence" not in result.stdout
    assert "analysis status: degraded" in result.stdout
    assert "tokens: 123" in result.stdout
    assert "Risk found." in result.stdout
    assert "must not be treated as a complete clean review" in result.stderr
    assert "Guardian analysis warnings: 1" in result.stderr
    assert "evidence path does not resolve" in result.stderr


def test_guardian_checkpoint_times_out_on_stale_report(tmp_path):
    repo, _head = _init_repo(tmp_path)
    guardian_dir = tmp_path / "guardian"
    guardian_dir.mkdir()
    (guardian_dir / "status.json").write_text(
        json.dumps(
            {
                "commit": "old",
                "running": False,
                "findings": 0,
                "llm_backend": "codex-sdk",
                "error": "",
            }
        ),
        encoding="utf-8",
    )
    (guardian_dir / "findings.md").write_text("# Old report\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_write_script(tmp_path)),
            "--repo",
            str(repo),
            "--guardian-dir",
            str(guardian_dir),
            "--timeout",
            "1",
            "--interval",
            "0.1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 5
    assert "timed out waiting" in result.stderr
    assert '"commit": "old"' in result.stderr


def test_guardian_checkpoint_starts_guardian_before_waiting(tmp_path):
    repo, baseline = _init_repo(tmp_path)
    baseline_file = tmp_path / "base_commit"
    baseline_file.write_text(baseline + "\n", encoding="utf-8")
    marker = tmp_path / "started"
    starter = tmp_path / "guardian-start"
    starter.write_text(
        f"#!/bin/sh\nprintf started > {marker}\n",
        encoding="utf-8",
    )
    starter.chmod(0o755)

    (repo / "mod.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "agent change")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    guardian_dir = tmp_path / "guardian"
    guardian_dir.mkdir()
    (guardian_dir / "status.json").write_text(
        json.dumps(
            {
                "commit": head,
                "running": False,
                "findings": 0,
                "analysis_status": "complete",
                "exit_reason": "ReportSubmitted",
                "llm_backend": "codex-sdk",
                "error": "",
            }
        ),
        encoding="utf-8",
    )
    (guardian_dir / "findings.md").write_text("# No findings\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(
                _write_script(
                    tmp_path,
                    start_command=str(starter),
                    baseline_file=str(baseline_file),
                )
            ),
            "--repo",
            str(repo),
            "--guardian-dir",
            str(guardian_dir),
            "--timeout",
            "1",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert marker.read_text(encoding="utf-8") == "started"
    assert "Guardian checkpoint: report ready" in result.stdout


def test_guardian_checkpoint_rejects_no_progress_as_incomplete(tmp_path):
    repo, head = _init_repo(tmp_path)
    guardian_dir = tmp_path / "guardian"
    guardian_dir.mkdir()
    (guardian_dir / "status.json").write_text(
        json.dumps(
            {
                "commit": head,
                "running": False,
                "findings": 0,
                "uncertain_specifications": 0,
                "degraded": False,
                "analysis_status": "incomplete",
                "exit_reason": "NoProgress",
                "llm_backend": "codex-sdk",
                "error": "",
            }
        ),
        encoding="utf-8",
    )
    (guardian_dir / "findings.md").write_text(
        "# Incomplete report\n\nNo findings were verified.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_write_script(tmp_path)),
            "--repo",
            str(repo),
            "--guardian-dir",
            str(guardian_dir),
            "--timeout",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 9
    assert "Guardian checkpoint: report incomplete" in result.stdout
    assert "analysis status: incomplete" in result.stdout
    assert "exit reason: NoProgress" in result.stdout
    assert "absence of findings is not a clean review" in result.stderr


def test_guardian_checkpoint_rejects_unchanged_baseline_without_waiting(tmp_path):
    repo, baseline = _init_repo(tmp_path)
    baseline_file = tmp_path / "base_commit"
    baseline_file.write_text(baseline + "\n", encoding="utf-8")
    marker = tmp_path / "started"
    starter = tmp_path / "guardian-start"
    starter.write_text(
        f"#!/bin/sh\nprintf started > {marker}\n",
        encoding="utf-8",
    )
    starter.chmod(0o755)

    result = subprocess.run(
        [
            sys.executable,
            str(
                _write_script(
                    tmp_path,
                    start_command=str(starter),
                    baseline_file=str(baseline_file),
                )
            ),
            "--repo",
            str(repo),
            "--guardian-dir",
            str(tmp_path / "guardian"),
            "--timeout",
            "30",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 8
    assert marker.read_text(encoding="utf-8") == "started"
    assert "repository baseline" in result.stderr


def test_guardian_checkpoint_publishes_bundle_and_reads_response(tmp_path):
    repo, baseline = _init_repo(tmp_path)
    baseline_file = tmp_path / "base_commit"
    baseline_file.write_text(baseline + "\n", encoding="utf-8")
    (repo / "mod.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "candidate")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    exchange = tmp_path / "exchange"
    script = _write_script(
        tmp_path,
        baseline_file=str(baseline_file),
        exchange_dir=str(exchange),
    )

    process = subprocess.Popen(
        [
            sys.executable,
            str(script),
            "--repo",
            str(repo),
            "--timeout",
            "5",
            "--interval",
            "0.05",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    manifest = exchange / "requests" / f"{head}.json"
    for _ in range(100):
        if manifest.exists():
            break
        import time

        time.sleep(0.05)
    assert manifest.exists()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["base_commit"] == baseline
    assert payload["candidate_commit"] == head
    assert (exchange / "bundles" / payload["bundle_name"]).is_file()

    response = exchange / "responses" / head
    response.mkdir(parents=True)
    (response / "findings.md").write_text("# Sandboxed report\n", encoding="utf-8")
    (response / "status.json").write_text(
        json.dumps(
            {
                "commit": head,
                "running": False,
                "findings": 0,
                "uncertain_specifications": 0,
                "analysis_status": "complete",
                "exit_reason": "ReviewCompleted",
                "llm_backend": "codex-cli+codenib-sandbox",
                "error": "",
            }
        ),
        encoding="utf-8",
    )
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, stderr
    assert "Sandboxed report" in stdout
    assert not (exchange / "last_reviewed_commit").exists()


def test_guardian_checkpoint_does_not_publish_after_terminal_cycle(tmp_path):
    repo, baseline = _init_repo(tmp_path)
    baseline_file = tmp_path / "base_commit"
    baseline_file.write_text(baseline + "\n", encoding="utf-8")
    (repo / "mod.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "reviewed candidate")
    reviewed = _git(repo, "rev-parse", "HEAD").stdout.strip()

    exchange = tmp_path / "exchange"
    latest = exchange / "latest"
    latest.mkdir(parents=True)
    (exchange / "last_reviewed_commit").write_text(reviewed + "\n", encoding="utf-8")
    (latest / "findings.md").write_text(
        "# Terminal report\n\nOne finding remains.\n", encoding="utf-8"
    )
    (latest / "status.json").write_text(
        json.dumps(
            {
                "commit": reviewed,
                "analysis_status": "complete",
                "exit_reason": "ReviewCompleted",
                "review_performed": True,
                "cycle_index": 3,
                "max_cycles": 3,
                "terminal": True,
                "termination_reason": "max_cycles_reached",
                "running": False,
                "error": "",
            }
        ),
        encoding="utf-8",
    )

    (repo / "mod.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "final unreviewed revision")
    final_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = subprocess.run(
        [
            sys.executable,
            str(
                _write_script(
                    tmp_path,
                    baseline_file=str(baseline_file),
                    exchange_dir=str(exchange),
                )
            ),
            "--repo",
            str(repo),
            "--timeout",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "review limit reached" in result.stdout
    assert f"current commit {final_head[:12]} was not reviewed" in result.stdout
    assert "One finding remains" in result.stdout
    assert not (exchange / "requests" / f"{final_head}.json").exists()
    assert (exchange / "last_reviewed_commit").read_text().strip() == reviewed
