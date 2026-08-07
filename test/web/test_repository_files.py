# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded immutable-repository reads used by the Web runtime."""

import subprocess
from pathlib import Path

import pytest

from codenib.web.repository_files import git_grep_paths, git_tree_paths


@pytest.fixture(autouse=True)
def _clear_snapshot_caches():
    git_grep_paths.cache_clear()
    git_tree_paths.cache_clear()
    yield
    git_grep_paths.cache_clear()
    git_tree_paths.cache_clear()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_git_snapshot_caches_evict_old_commits(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    commits = []
    for revision in range(3):
        (tmp_path / "source.py").write_text(
            f"# generated revision {revision}\nVALUE = {revision}\n"
        )
        _git(tmp_path, "add", "source.py")
        _git(tmp_path, "commit", "-qm", f"revision {revision}")
        commits.append(_git(tmp_path, "rev-parse", "HEAD"))

    for commit in commits:
        assert git_tree_paths(str(tmp_path), commit) == frozenset({"source.py"})
        assert git_grep_paths(str(tmp_path), commit, "generated") == frozenset(
            {"source.py"}
        )

    assert git_tree_paths.cache_info().currsize == 2
    assert git_grep_paths.cache_info().currsize == 2

    tree_misses = git_tree_paths.cache_info().misses
    grep_misses = git_grep_paths.cache_info().misses
    git_tree_paths(str(tmp_path), commits[0])
    git_grep_paths(str(tmp_path), commits[0], "generated")
    assert git_tree_paths.cache_info().misses == tree_misses + 1
    assert git_grep_paths.cache_info().misses == grep_misses + 1
