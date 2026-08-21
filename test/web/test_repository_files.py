# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import capture_repository_source
from codenib.web.repository_files import (
    bound_source_slice,
    git_grep_paths,
    git_tree_paths,
    live_source_slice,
)


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
            f"# generated revision {revision}\nVALUE = {revision}\n",
            encoding="utf-8",
        )
        _git(tmp_path, "add", "source.py")
        _git(tmp_path, "commit", "-qm", f"revision {revision}")
        commits.append(_git(tmp_path, "rev-parse", "HEAD"))

    for commit in commits:
        assert git_tree_paths(str(tmp_path), commit) == frozenset({"source.py"})
        assert git_grep_paths(str(tmp_path), commit, "generated") == frozenset(
            {"source.py"}
        )

    assert git_tree_paths.cache_info().maxsize == 2
    assert git_tree_paths.cache_info().currsize == 2
    assert git_grep_paths.cache_info().maxsize == 2
    assert git_grep_paths.cache_info().currsize == 2

    tree_misses = git_tree_paths.cache_info().misses
    grep_misses = git_grep_paths.cache_info().misses
    git_tree_paths(str(tmp_path), commits[0])
    git_grep_paths(str(tmp_path), commits[0], "generated")
    assert git_tree_paths.cache_info().misses == tree_misses + 1
    assert git_grep_paths.cache_info().misses == grep_misses + 1


def test_live_source_slice_bounds_explicit_line_ranges(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_text(
        "".join(f"line_{line}\n" for line in range(1, 2_001)),
        encoding="utf-8",
    )

    result = live_source_slice(tmp_path, "large.py", start=1, end=2_000)

    assert result is not None
    assert result["start_line"] == 1
    assert result["end_line"] == 1_000
    assert len(result["content"].splitlines()) == 1_000


def test_live_source_slice_applies_default_window_from_start(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_text(
        "".join(f"line_{line}\n" for line in range(1, 1_001)),
        encoding="utf-8",
    )

    result = live_source_slice(tmp_path, "large.py", start=501)

    assert result is not None
    assert result["start_line"] == 501
    assert result["end_line"] == 900
    assert result["content"].startswith("line_501\n")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_live_source_slice_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "source.fifo"
    os.mkfifo(fifo)

    started = time.monotonic()
    result = live_source_slice(tmp_path, "source.fifo")

    assert result is None
    assert time.monotonic() - started < 1


def test_bound_source_slice_uses_authenticated_selection(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "runtime.py").write_text(
        "first\nsecond\nthird\n",
        encoding="utf-8",
    )
    (tmp_path / "excluded").mkdir()
    (tmp_path / "excluded" / "secret.py").write_text(
        "SECRET = 'not selected'\n",
        encoding="utf-8",
    )
    binding = capture_repository_source(
        tmp_path,
        selection=RepositorySourceSelection(("excluded",)),
    )
    try:
        reader = binding.borrow_reader()

        assert bound_source_slice(reader, "src/runtime.py", 2, 3) == {
            "file": "src/runtime.py",
            "start_line": 2,
            "end_line": 3,
            "content": "second\nthird\n",
        }
        assert bound_source_slice(reader, "excluded/secret.py") is None
    finally:
        binding.close()
