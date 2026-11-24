# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A local Wiki follows its working tree."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codenib.web.worktree import WorktreeFollower


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "maintenance.auto=false", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "calc.py").write_text("x = 1\n")
    _git(path, "add", ".")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return path


def _follower(repo: Path, tmp_path: Path, code: str = "0"):
    updates = []
    follower = WorktreeFollower(
        repo,
        [sys.executable, "-c", f"import sys; sys.exit({code})"],
        on_updated=updates.append,
        before_update=lambda: updates.append("snapshot"),
        log_path=tmp_path / "reindex.log",
    )
    return follower, updates


def test_a_change_is_indexed_once_it_holds_still(repo: Path, tmp_path: Path) -> None:
    follower, updates = _follower(repo, tmp_path)
    assert follower.step() is False and updates == []  # nothing changed

    (repo / "calc.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 1\n")
    assert follower.step() is False  # first sight: wait one poll
    assert follower.step() is True

    assert updates == ["snapshot", {"calc.py", "new.py"}]
    assert follower.status()["state"] == "current"
    assert follower.status()["updated_at"]
    assert follower.step() is False  # up to date


def test_annotation_files_do_not_count(repo: Path, tmp_path: Path) -> None:
    follower, updates = _follower(repo, tmp_path)
    notes = repo / ".codenib" / "notes"
    notes.mkdir(parents=True)
    (notes / "x.md").write_text("note\n")
    assert follower.step() is False and follower.step() is False
    assert updates == []


def test_a_commit_names_the_files_it_changed(repo: Path, tmp_path: Path) -> None:
    follower, updates = _follower(repo, tmp_path)
    (repo / "calc.py").write_text("x = 3\n")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "edit")
    follower.step()
    assert follower.step() is True
    assert updates[-1] == {"calc.py"}


def test_a_failed_update_waits_for_the_next_change(repo: Path, tmp_path: Path) -> None:
    follower, updates = _follower(repo, tmp_path, code="3")
    (repo / "calc.py").write_text("x = 4\n")
    follower.step()
    assert follower.step() is False
    status = follower.status()
    assert status["state"] == "failed" and "exit 3" in status["error"]
    assert follower.step() is False  # the same tree is not retried
    assert updates == ["snapshot"]

    follower.argv = [sys.executable, "-c", "pass"]
    (repo / "calc.py").write_text("x = 5\n")
    follower.step()
    assert follower.step() is True
    assert follower.status()["state"] == "current"
