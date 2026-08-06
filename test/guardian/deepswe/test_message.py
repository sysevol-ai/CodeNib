# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the standalone Guardian message action."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.guardian.deepswe.harness.message import guardian_message_script


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_message_action_appends_solver_context_without_codenib(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "solver@example.com")
    _git(repo, "config", "user.name", "Solver")
    (repo / "file.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "file.py")
    _git(repo, "commit", "--quiet", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD")
    inbox = tmp_path / "exchange" / "messages.jsonl"
    script = tmp_path / "guardian-message"
    script.write_text(
        guardian_message_script(inbox=str(inbox), repo=str(repo)),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), "--scope", "file.py"],
        input="The public path may need the same behavior.",
        capture_output=True,
        text=True,
        check=True,
    )

    value = json.loads(result.stdout)
    assert value["content"] == "The public path may need the same behavior."
    assert value["scope"] == ["file.py"]
    assert value["observed_head"] == head
    assert json.loads(inbox.read_text())["id"] == "msg_00000001"
