# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for git-based change detection in the incremental patcher."""

import subprocess
from pathlib import Path

from codenib.graph.incremental.change_mgr import detect_changed_files


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_non_ascii_names_are_not_c_quoted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name in ("café.py", "plain.py", "old_é.py", "gone_é.py"):
        (repo / name).write_text(f"# {name}\n" * 5)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    first = _git(repo, "rev-parse", "HEAD")

    (repo / "café.py").write_text("x = 2\n")
    (repo / "new_é.py").write_text("y = 1\n")
    (repo / "gone_é.py").unlink()
    _git(repo, "mv", "old_é.py", "moved_é.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")
    second = _git(repo, "rev-parse", "HEAD")

    changed = detect_changed_files(str(repo), first, second)

    assert changed["modified"] == ["café.py"]
    assert changed["added"] == ["new_é.py"]
    assert changed["deleted"] == ["gone_é.py"]
    assert changed["renamed"] == [("old_é.py", "moved_é.py")]
