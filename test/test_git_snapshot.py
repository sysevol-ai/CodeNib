# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess

import pytest

from codenib.git_snapshot import GitSourceSurface, normalize_repository_path


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def test_git_source_surface_classifies_commit_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "src/main.py")
    _git(repo, "commit", "-m", "initial")

    surface = GitSourceSurface.load(repo)

    assert surface.contains("src/main.py")
    assert surface.contains("./src/main.py")
    assert not surface.contains("lib/generated.py")
    assert surface.classify(["src/main.py", "lib/generated.py"]) == {
        "tracked": ("src/main.py",),
        "submodule": (),
        "outside": ("lib/generated.py",),
    }


@pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py", ""])
def test_normalize_repository_path_rejects_escape(path):
    with pytest.raises(ValueError):
        normalize_repository_path(path)
