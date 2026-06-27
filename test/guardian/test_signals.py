# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codeminer.guardian.signals (no external deps)."""

import subprocess

from codeminer.guardian.signals import Hotspot, churn_hotspots


def _git(repo, *args):
    env = {
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**_base_env(), **env},
    )


def _base_env():
    import os

    return dict(os.environ)


def _commit_file(repo, name, content):
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"touch {name}")


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    return repo


def test_churn_ranks_by_commit_count(tmp_path):
    repo = _make_repo(tmp_path)
    # hot.py changed in 3 commits, warm.py in 2, cold.py in 1.
    for i in range(3):
        _commit_file(repo, "hot.py", f"# v{i}\n")
    for i in range(2):
        _commit_file(repo, "warm.py", f"# v{i}\n")
    _commit_file(repo, "cold.py", "# v0\n")

    hotspots = churn_hotspots(str(repo), since="10 years ago", top_n=10)
    paths = [h.path for h in hotspots]

    assert paths[:3] == ["hot.py", "warm.py", "cold.py"]
    assert hotspots[0] == Hotspot(path="hot.py", commit_count=3)


def test_churn_respects_top_n(tmp_path):
    repo = _make_repo(tmp_path)
    for name in ("a.py", "b.py", "c.py"):
        _commit_file(repo, name, "x\n")

    hotspots = churn_hotspots(str(repo), since="10 years ago", top_n=2)
    assert len(hotspots) == 2


def test_churn_skips_non_code_and_deleted(tmp_path):
    repo = _make_repo(tmp_path)
    _commit_file(repo, "keep.py", "x\n")
    _commit_file(repo, "notes.txt", "doc\n")  # non-code extension -> excluded
    _commit_file(repo, "gone.py", "x\n")
    _git(repo, "rm", "gone.py")
    _git(repo, "commit", "-m", "remove gone")

    paths = [h.path for h in churn_hotspots(str(repo), since="10 years ago")]
    assert "keep.py" in paths
    assert "notes.txt" not in paths  # filtered by extension
    assert "gone.py" not in paths  # no longer on disk


def test_churn_non_git_dir_returns_empty(tmp_path):
    assert churn_hotspots(str(tmp_path)) == []
