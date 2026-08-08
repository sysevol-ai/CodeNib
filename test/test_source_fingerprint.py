# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess

import pytest

import codenib._contained_source as contained_source_module
from codenib.source_fingerprint import (
    RepositoryChangedError,
    fingerprint_repository,
    repository_source_is_dirty,
)


def test_fingerprint_changes_with_source_content_and_path(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    initial = fingerprint_repository(tmp_path)

    source.write_text("VALUE = 2\n")
    changed_content = fingerprint_repository(tmp_path)
    assert changed_content.value != initial.value
    assert changed_content.file_count == 1

    source.rename(tmp_path / "renamed.py")
    changed_path = fingerprint_repository(tmp_path)
    assert changed_path.value != changed_content.value


def test_fingerprint_ignores_generated_and_explicit_artifact_roots(tmp_path):
    (tmp_path / "module.py").write_text("VALUE = 1\n")
    initial = fingerprint_repository(tmp_path)

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "wheel.whl").write_bytes(b"generated")
    cache = tmp_path / "custom-cache"
    cache.mkdir()
    (cache / "index.bin").write_bytes(b"artifact")

    assert (
        fingerprint_repository(tmp_path, exclude_roots=(cache,)).value == initial.value
    )


def test_fingerprint_ignores_git_worktree_pointer_file(tmp_path):
    (tmp_path / "module.py").write_text("VALUE = 1\n")
    initial = fingerprint_repository(tmp_path)

    (tmp_path / ".git").write_text("gitdir: /shared/repository/.git/worktrees/test\n")

    observed = fingerprint_repository(tmp_path)
    assert observed == initial


def test_fingerprint_includes_symlink_target_and_content(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.py"
    target.write_text("VALUE = 1\n")
    link = repo / "current.py"
    link.symlink_to("target.py")
    initial = fingerprint_repository(repo)

    target.write_text("VALUE = 2\n")
    changed_content = fingerprint_repository(repo)
    assert changed_content.value != initial.value

    link.unlink()
    (repo / "other.py").write_text("VALUE = 3\n")
    link.symlink_to("other.py")
    assert fingerprint_repository(repo).value != changed_content.value


def test_fingerprint_rejects_source_symlink_outside_repository(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.py").write_text("SECRET = 1\n")
    (repo / "current.py").symlink_to("../outside.py")

    with pytest.raises(RepositoryChangedError, match="could not be read consistently"):
        fingerprint_repository(repo)


def test_fingerprint_rejects_reversible_source_symlink_swap(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "target.py").write_text("VALUE = 1\n")
    (tmp_path / "outside.py").write_text("SECRET = 1\n")
    link = repo / "current.py"
    link.symlink_to("target.py")
    real_verify = contained_source_module._BoundRepositoryFile.verify
    calls = 0

    def swap_then_restore(binding) -> None:
        nonlocal calls
        calls += 1
        if calls != 2:
            return real_verify(binding)
        link.unlink()
        link.symlink_to("../outside.py")
        try:
            return real_verify(binding)
        finally:
            link.unlink()
            link.symlink_to("target.py")

    monkeypatch.setattr(
        contained_source_module._BoundRepositoryFile,
        "verify",
        swap_then_restore,
    )

    with pytest.raises(RepositoryChangedError, match="could not be read consistently"):
        fingerprint_repository(repo)


def test_dirty_check_ignores_generated_dirs_but_detects_source_changes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "module.py"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "initial"],
        check=True,
    )

    assert repository_source_is_dirty(tmp_path) is False

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "generated.js").write_text("output\n")
    assert repository_source_is_dirty(tmp_path) is False

    source.write_text("VALUE = 2\n")
    assert repository_source_is_dirty(tmp_path) is True
