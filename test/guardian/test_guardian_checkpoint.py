# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Guardian final checkpoint script."""

from __future__ import annotations

import json
import subprocess
import sys

from deepsweguardian.checkpoint import guardian_checkpoint_script


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


def _write_script(tmp_path, *, start_command="", baseline_file=""):
    path = tmp_path / "guardian-checkpoint"
    path.write_text(
        guardian_checkpoint_script(
            start_command=start_command,
            baseline_file=baseline_file,
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
                "llm_model": "codex:gpt-5.6-luna",
                "llm_backend": "codex-sdk",
                "llm_tokens": {"total": 123},
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
    assert "backend: codex-sdk" in result.stdout
    assert "tokens: 123" in result.stdout
    assert "Risk found." in result.stdout


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
