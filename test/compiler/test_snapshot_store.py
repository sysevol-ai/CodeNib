# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess

import pytest

from codenib.compiler.snapshot_store import (
    ArtifactProfile,
    SnapshotArtifactStore,
    SourceSnapshot,
    normalize_repo,
)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _make_repo(path):
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / ".gitignore").write_text(
        "lib/\nnode_modules/\ncompile_commands.json\n*.so\n",
        encoding="utf-8",
    )
    _git(path, "add", "main.py", ".gitignore")
    _git(path, "commit", "-m", "initial")
    return _git(path, "rev-parse", "HEAD")


def test_normalize_repo_accepts_slug_and_git_urls():
    assert normalize_repo("Org/Repo") == "org/repo"
    assert normalize_repo("https://github.com/Org/Repo.git") == "org/repo"
    assert normalize_repo("git@github.com:Org/Repo.git") == "org/repo"


def test_bind_reuses_snapshot_and_profile_across_instances(tmp_path):
    store = SnapshotArtifactStore(tmp_path / "artifacts")
    snapshot = SourceSnapshot("org/repo", "a" * 40)
    profile = ArtifactProfile.create(["python"], name="paper-v1")

    first = store.bind("org__repo-1", snapshot, profile)
    second = store.bind("org__repo-2", snapshot, profile)

    assert first.snapshot_hit is False
    assert first.profile_hit is False
    assert second.snapshot_hit is True
    assert second.profile_hit is True
    assert first.profile_dir == second.profile_dir
    assert first.instance_path.resolve() == second.instance_path.resolve()
    assert store.resolve("org__repo-1") == first.profile_dir


def test_bind_keeps_profiles_separate(tmp_path):
    store = SnapshotArtifactStore(tmp_path / "artifacts")
    snapshot = SourceSnapshot("org/repo", "b" * 40)

    python = store.bind(
        "org__repo-python",
        snapshot,
        ArtifactProfile.create(["python"], name="paper-v1"),
    )
    java = store.bind(
        "org__repo-java",
        snapshot,
        ArtifactProfile.create(["java"], name="paper-v1"),
    )

    assert python.snapshot_dir == java.snapshot_dir
    assert python.profile_dir != java.profile_dir


def test_rebinding_instance_to_different_snapshot_fails(tmp_path):
    store = SnapshotArtifactStore(tmp_path / "artifacts")
    profile = ArtifactProfile.create(["python"])
    store.bind("org__repo-1", SourceSnapshot("org/repo", "a" * 40), profile)

    with pytest.raises(ValueError, match="another profile"):
        store.bind("org__repo-1", SourceSnapshot("org/repo", "b" * 40), profile)


def test_ensure_worktree_is_durable_and_idempotent(tmp_path):
    source = tmp_path / "source"
    commit = _make_repo(source)
    store = SnapshotArtifactStore(tmp_path / "artifacts")
    binding = store.bind(
        "org__repo-1",
        SourceSnapshot("org/repo", commit),
        ArtifactProfile.create(["python"]),
    )

    worktree = store.ensure_worktree(binding, source_repo=source, commit=commit)
    again = store.ensure_worktree(binding, source_repo=source, commit=commit)

    assert again == worktree
    assert (worktree / "main.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(worktree, "rev-parse", "HEAD") == commit
    assert (binding.profile_dir / "repo").resolve() == worktree


def test_ensure_worktree_removes_visible_generated_files_but_keeps_tool_cache(
    tmp_path,
):
    source = tmp_path / "source"
    commit = _make_repo(source)
    store = SnapshotArtifactStore(tmp_path / "artifacts")
    binding = store.bind(
        "org__repo-1",
        SourceSnapshot("org/repo", commit),
        ArtifactProfile.create(["python"]),
    )
    worktree = store.ensure_worktree(binding, source_repo=source, commit=commit)

    (worktree / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    (worktree / "scratch.py").write_text("temporary\n", encoding="utf-8")
    (worktree / "lib").mkdir()
    (worktree / "lib" / "generated.py").write_text("generated\n", encoding="utf-8")
    compile_commands = worktree / "compile_commands.json"
    compile_commands.write_text("[]\n", encoding="utf-8")
    extension = worktree / "codenib_core.so"
    extension.write_bytes(b"compiled-extension")
    (worktree / "node_modules" / "pkg").mkdir(parents=True)
    cached = worktree / "node_modules" / "pkg" / "index.js"
    cached.write_text("cached\n", encoding="utf-8")

    store.ensure_worktree(binding, source_repo=source, commit=commit)

    assert (worktree / "main.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (worktree / "scratch.py").exists()
    assert not (worktree / "lib").exists()
    assert compile_commands.read_text(encoding="utf-8") == "[]\n"
    assert extension.read_bytes() == b"compiled-extension"
    assert cached.read_text(encoding="utf-8") == "cached\n"
