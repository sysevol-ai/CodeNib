# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for GitDiffDetector.

Uses a temporary git repository created in-memory to avoid network
dependencies and to fully control the commit history.
"""

import subprocess
from pathlib import Path

import pytest

from codeminer.index.incremental.git_diff import GitDiffDetector

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _run(cmd: list, cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def git_repo(tmp_path: Path):
    """
    Create a minimal git repository with two commits:
      Commit A (initial): adds foo.py and bar.rs
      Commit B (second):  modifies foo.py, adds baz.ts, deletes bar.rs,
                           adds README.md (non-code file, should be ignored)
    Returns dict with keys: repo_path, commit_a, commit_b
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    _run(["git", "init"], str(repo))
    _run(["git", "config", "user.email", "test@example.com"], str(repo))
    _run(["git", "config", "user.name", "Test"], str(repo))

    # Commit A
    (repo / "foo.py").write_text("def hello():\n    return 'hello'\n")
    (repo / "bar.rs").write_text("fn main() {}\n")
    _run(["git", "add", "."], str(repo))
    _run(["git", "commit", "-m", "initial"], str(repo))
    commit_a = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()

    # Commit B
    (repo / "foo.py").write_text("def hello():\n    return 'world'\n")
    (repo / "baz.ts").write_text("export const x = 1;\n")
    (repo / "README.md").write_text("# Docs\n")
    (repo / "bar.rs").unlink()
    _run(["git", "add", "."], str(repo))
    _run(["git", "commit", "-m", "second"], str(repo))
    commit_b = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()

    return {"repo_path": str(repo), "commit_a": commit_a, "commit_b": commit_b}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetCurrentCommit:
    def test_returns_sha_for_git_repo(self, git_repo):
        detector = GitDiffDetector()
        sha = detector.get_current_commit(git_repo["repo_path"])
        assert sha == git_repo["commit_b"]

    def test_returns_empty_for_non_git_dir(self, tmp_path):
        detector = GitDiffDetector()
        sha = detector.get_current_commit(str(tmp_path))
        assert sha == ""


class TestDetectChanges:
    def test_detects_modified_file(self, git_repo):
        detector = GitDiffDetector()
        changes = detector.detect_changes(
            git_repo["repo_path"], since_commit=git_repo["commit_a"]
        )
        modified_basenames = [Path(p).name for p in changes.modified]
        assert "foo.py" in modified_basenames

    def test_detects_added_file(self, git_repo):
        detector = GitDiffDetector()
        changes = detector.detect_changes(
            git_repo["repo_path"], since_commit=git_repo["commit_a"]
        )
        added_basenames = [Path(p).name for p in changes.added]
        assert "baz.ts" in added_basenames

    def test_detects_deleted_file(self, git_repo):
        detector = GitDiffDetector()
        changes = detector.detect_changes(
            git_repo["repo_path"], since_commit=git_repo["commit_a"]
        )
        deleted_basenames = [Path(p).name for p in changes.deleted]
        assert "bar.rs" in deleted_basenames

    def test_ignores_non_code_files(self, git_repo):
        """README.md should not appear in any change list."""
        detector = GitDiffDetector()
        changes = detector.detect_changes(
            git_repo["repo_path"], since_commit=git_repo["commit_a"]
        )
        all_paths = changes.added + changes.modified + changes.deleted
        all_basenames = [Path(p).name for p in all_paths]
        assert "README.md" not in all_basenames

    def test_returns_absolute_paths(self, git_repo):
        detector = GitDiffDetector()
        changes = detector.detect_changes(
            git_repo["repo_path"], since_commit=git_repo["commit_a"]
        )
        for p in changes.affected + changes.deleted:
            assert Path(p).is_absolute()

    def test_empty_when_no_changes(self, git_repo):
        detector = GitDiffDetector()
        changes = detector.detect_changes(
            git_repo["repo_path"], since_commit=git_repo["commit_b"]
        )
        assert changes.is_empty

    def test_commit_sha_recorded(self, git_repo):
        detector = GitDiffDetector()
        changes = detector.detect_changes(
            git_repo["repo_path"], since_commit=git_repo["commit_a"]
        )
        assert changes.old_commit == git_repo["commit_a"]
        assert changes.new_commit == git_repo["commit_b"]

    def test_custom_extension_filter(self, git_repo):
        """With only .py in supported_extensions, .rs and .ts changes are ignored."""
        detector = GitDiffDetector(supported_extensions={".py"})
        changes = detector.detect_changes(
            git_repo["repo_path"], since_commit=git_repo["commit_a"]
        )
        all_basenames = [
            Path(p).name for p in changes.added + changes.modified + changes.deleted
        ]
        assert "foo.py" in all_basenames
        assert "baz.ts" not in all_basenames
        assert "bar.rs" not in all_basenames

    def test_rename_deletes_old_path(self, tmp_path):
        """
        Git renames must produce a delete for the old path AND a modify for
        the new path, so the chunk store cleans up the old path's entries.

        Regression: without the delete, old-path chunks and FAISS vectors
        remain as ghost entries.
        """
        repo = tmp_path / "rename_repo"
        repo.mkdir()
        _run(["git", "init"], str(repo))
        _run(["git", "config", "user.email", "t@t.com"], str(repo))
        _run(["git", "config", "user.name", "T"], str(repo))

        (repo / "old_name.py").write_text("def f():\n    pass\n")
        _run(["git", "add", "."], str(repo))
        _run(["git", "commit", "-m", "initial"], str(repo))
        commit_a = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        _run(["git", "mv", "old_name.py", "new_name.py"], str(repo))
        _run(["git", "commit", "-m", "rename"], str(repo))

        detector = GitDiffDetector(supported_extensions={".py"})
        changes = detector.detect_changes(str(repo), commit_a)

        deleted_basenames = [Path(p).name for p in changes.deleted]
        modified_or_added = [Path(p).name for p in changes.modified + changes.added]

        assert (
            "old_name.py" in deleted_basenames
        ), f"Old path should be in deleted list, got deleted={deleted_basenames}"
        assert (
            "new_name.py" in modified_or_added
        ), f"New path should be in modified/added list, got={modified_or_added}"
