# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess

import pytest

from codenib.git_snapshot import (
    GitSourceSurface,
    normalize_repository_path,
    restore_git_worktree,
)


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


def test_restore_git_worktree_hydrates_recorded_submodule_commit(tmp_path):
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    _git(dependency, "init")
    _git(dependency, "config", "user.email", "test@example.com")
    _git(dependency, "config", "user.name", "Test User")
    (dependency / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(dependency, "add", "value.py")
    _git(dependency, "commit", "-m", "first")
    first = _git(dependency, "rev-parse", "HEAD")
    (dependency / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(dependency, "commit", "-am", "second")
    second = _git(dependency, "rev-parse", "HEAD")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(dependency),
        "vendor/dependency",
    )
    submodule = repo / "vendor" / "dependency"
    _git(submodule, "checkout", "--detach", first)
    _git(repo, "add", ".gitmodules", "vendor/dependency")
    _git(repo, "commit", "-m", "pin dependency")
    parent_commit = _git(repo, "rev-parse", "HEAD")

    _git(submodule, "checkout", "--detach", second)
    assert (submodule / "value.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    restore_git_worktree(repo, parent_commit)

    assert _git(submodule, "rev-parse", "HEAD") == first
    assert (submodule / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    _git(repo, "submodule", "deinit", "--force", "vendor/dependency")
    assert not (submodule / "value.py").exists()

    restore_git_worktree(repo, parent_commit)

    assert _git(submodule, "rev-parse", "HEAD") == first
    assert (submodule / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
