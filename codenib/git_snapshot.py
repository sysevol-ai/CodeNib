# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Git-backed source boundaries for reproducible repository artifacts."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .repository_filters import repository_path_is_visible


def normalize_repository_path(path: str | Path) -> str:
    """Return a validated POSIX repository-relative path."""

    value = os.fspath(path).replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    normalized = PurePosixPath(value)
    if not normalized.parts or normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"path must stay repository-relative: {path!r}")
    return normalized.as_posix()


def _git(
    repo: str | Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    root = Path(repo).expanduser().resolve()
    return subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root}",
            "-C",
            str(root),
            *args,
        ],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def resolve_git_commit(repo: str | Path, commit: str = "HEAD") -> str:
    """Resolve *commit* to its full immutable commit object ID."""

    result = _git(repo, "rev-parse", f"{commit}^{{commit}}")
    return result.stdout.decode("ascii").strip().lower()


@dataclass(frozen=True, slots=True)
class GitSourceSurface:
    """Paths addressable by one Git commit, including gitlink subtrees."""

    commit: str
    tree: str
    tracked_files: frozenset[str]
    submodules: tuple[tuple[str, str], ...]

    @classmethod
    def load(cls, repo: str | Path, commit: str = "HEAD") -> "GitSourceSurface":
        resolved = resolve_git_commit(repo, commit)
        tree = (
            _git(repo, "rev-parse", f"{resolved}^{{tree}}")
            .stdout.decode("ascii")
            .strip()
            .lower()
        )
        records = _git(repo, "ls-tree", "-r", "-z", "--full-tree", resolved).stdout
        tracked_files: set[str] = set()
        submodules: list[tuple[str, str]] = []
        for record in records.split(b"\0"):
            if not record:
                continue
            metadata, separator, encoded_path = record.partition(b"\t")
            if not separator:
                raise ValueError("invalid git ls-tree record")
            mode, object_type, object_id = metadata.decode("ascii").split()
            path = normalize_repository_path(os.fsdecode(encoded_path))
            if mode == "160000" or object_type == "commit":
                submodules.append((path, object_id.lower()))
            elif object_type == "blob":
                tracked_files.add(path)
        return cls(
            commit=resolved,
            tree=tree,
            tracked_files=frozenset(tracked_files),
            submodules=tuple(sorted(submodules)),
        )

    @property
    def submodule_roots(self) -> tuple[str, ...]:
        return tuple(path for path, _commit in self.submodules)

    def contains(self, path: str | Path) -> bool:
        """Whether *path* belongs to this commit's source address space."""

        normalized = normalize_repository_path(path)
        if normalized in self.tracked_files:
            return True
        return any(
            normalized == root or normalized.startswith(f"{root}/")
            for root, _commit in self.submodules
        )

    def has_descendant(self, path: str | Path) -> bool:
        """Whether *path* is an ancestor of an addressable commit path."""

        normalized = normalize_repository_path(path)
        prefix = f"{normalized}/"
        return any(item.startswith(prefix) for item in self.tracked_files) or any(
            root == normalized
            or root.startswith(prefix)
            or normalized.startswith(f"{root}/")
            for root, _commit in self.submodules
        )

    def classify(self, paths: Iterable[str | Path]) -> dict[str, tuple[str, ...]]:
        """Classify paths as tracked, submodule-owned, or outside the commit."""

        tracked: set[str] = set()
        submodule: set[str] = set()
        outside: set[str] = set()
        for path in paths:
            normalized = normalize_repository_path(path)
            if normalized in self.tracked_files:
                tracked.add(normalized)
            elif any(
                normalized == root or normalized.startswith(f"{root}/")
                for root, _commit in self.submodules
            ):
                submodule.add(normalized)
            else:
                outside.add(normalized)
        return {
            "tracked": tuple(sorted(tracked)),
            "submodule": tuple(sorted(submodule)),
            "outside": tuple(sorted(outside)),
        }


@dataclass(frozen=True, slots=True)
class WorktreeRestoreResult:
    """Outcome of restoring one checkout to an immutable source boundary."""

    commit: str
    removed_ignored_paths: tuple[str, ...]


def _ignored_visible_paths(repo: Path) -> tuple[str, ...]:
    result = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
    )
    paths: set[str] = set()
    for record in result.stdout.split(b"\0"):
        if len(record) < 4 or record[:2] != b"!!":
            continue
        path = normalize_repository_path(os.fsdecode(record[3:]))
        if repository_path_is_visible(path):
            paths.add(path)

    # Removing an ignored directory also removes any nested status records.
    selected: list[str] = []
    for path in sorted(paths, key=lambda value: (value.count("/"), value)):
        if any(path.startswith(f"{parent}/") for parent in selected):
            continue
        selected.append(path)
    return tuple(selected)


def _remove_relative_path(repo: Path, relative: str) -> None:
    target = repo.joinpath(*PurePosixPath(relative).parts)
    try:
        target.relative_to(repo)
    except ValueError as exc:
        raise ValueError(
            f"refusing to remove path outside repository: {relative}"
        ) from exc
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
    elif target.is_dir():
        shutil.rmtree(target)


def restore_git_worktree(
    repo: str | Path,
    commit: str,
) -> WorktreeRestoreResult:
    """Restore tracked source and remove source-visible generated files.

    Ignored dependency/tool caches excluded by the shared repository traversal
    policy are retained. Ignored paths that an index builder would otherwise
    observe are removed so artifacts cannot silently include build products
    left by another benchmark instance.
    """

    root = Path(repo).expanduser().resolve()
    expected = resolve_git_commit(root, commit)
    _git(root, "checkout", "--detach", "--force", expected)
    _git(root, "reset", "--hard", expected)
    _git(root, "clean", "-fd")

    ignored = _ignored_visible_paths(root)
    for relative in ignored:
        _remove_relative_path(root, relative)

    actual = resolve_git_commit(root)
    if actual != expected:
        raise RuntimeError(
            f"worktree commit mismatch after restore: expected {expected}, found {actual}"
        )
    remaining = _ignored_visible_paths(root)
    if remaining:
        raise RuntimeError(
            "source-visible ignored paths remain after restore: " + ", ".join(remaining)
        )
    return WorktreeRestoreResult(
        commit=expected,
        removed_ignored_paths=ignored,
    )


__all__ = [
    "GitSourceSurface",
    "WorktreeRestoreResult",
    "normalize_repository_path",
    "resolve_git_commit",
    "restore_git_worktree",
]
