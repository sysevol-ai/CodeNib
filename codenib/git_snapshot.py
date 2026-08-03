# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Git-backed source boundaries for reproducible repository artifacts."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable

from .languages import extension_to_language_map
from .repository_filters import repository_path_is_visible

_SOURCE_SUFFIXES = frozenset(extension_to_language_map("chunker"))


@lru_cache(maxsize=32768)
def _normalize_repository_path(value: str) -> str:
    value = value.replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    normalized = PurePosixPath(value)
    if not normalized.parts or normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"path must stay repository-relative: {value!r}")
    return normalized.as_posix()


def normalize_repository_path(path: str | Path) -> str:
    """Return a validated POSIX repository-relative path."""

    return _normalize_repository_path(os.fspath(path))


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


def _visible_source_files(repo: Path, relative: str) -> tuple[str, ...]:
    """Expand one ignored status path to source files visible to indexers."""

    target = repo.joinpath(*PurePosixPath(relative).parts)
    if target.is_symlink() or target.is_file():
        return (relative,) if Path(relative).suffix in _SOURCE_SUFFIXES else ()
    if not target.is_dir():
        return ()

    files: list[str] = []
    for current_root, dirs, names in os.walk(target, followlinks=False):
        current = Path(current_root)
        dirs[:] = sorted(
            name
            for name in dirs
            if repository_path_is_visible((current / name).relative_to(repo))
        )
        for name in sorted(names):
            path = current / name
            source_path = path.relative_to(repo).as_posix()
            if Path(name).suffix in _SOURCE_SUFFIXES and repository_path_is_visible(
                source_path
            ):
                files.append(source_path)
    return tuple(files)


def _ignored_visible_source_paths(repo: Path) -> tuple[str, ...]:
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
            paths.update(_visible_source_files(repo, path))
    return tuple(sorted(paths))


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
        raise RuntimeError(f"refusing to recursively remove source path: {relative}")

    parent = target.parent
    while parent != repo:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _restore_git_submodules(root: Path, surface: GitSourceSurface) -> None:
    if not surface.submodules:
        return

    _git(root, "submodule", "sync", "--recursive")
    _git(root, "submodule", "update", "--init", "--recursive", "--force")
    _git(
        root,
        "submodule",
        "foreach",
        "--quiet",
        "--recursive",
        "git reset --hard && git clean -fd",
    )
    mismatches = []
    for relative, expected in surface.submodules:
        submodule_root = root.joinpath(*PurePosixPath(relative).parts)
        try:
            actual = resolve_git_commit(submodule_root)
        except (OSError, subprocess.CalledProcessError):
            actual = "missing"
        if actual != expected:
            mismatches.append(f"{relative}: expected {expected}, found {actual}")
    if mismatches:
        raise RuntimeError(
            "submodule commit mismatch after restore: " + "; ".join(mismatches)
        )


def restore_git_worktree(
    repo: str | Path,
    commit: str,
) -> WorktreeRestoreResult:
    """Restore tracked source, gitlinks, and source-visible generated files.

    Ignored dependency/tool caches and non-source build inputs are retained.
    Ignored source files that an index builder would otherwise observe are
    removed so artifacts cannot silently include generated code left by another
    benchmark instance. Recorded submodules are initialized recursively and
    reset to the commits referenced by the restored superproject.
    """

    root = Path(repo).expanduser().resolve()
    expected = resolve_git_commit(root, commit)
    _git(root, "checkout", "--detach", "--force", expected)
    _git(root, "reset", "--hard", expected)
    _git(root, "clean", "-fd")
    surface = GitSourceSurface.load(root, expected)
    _restore_git_submodules(root, surface)

    ignored = _ignored_visible_source_paths(root)
    for relative in ignored:
        _remove_relative_path(root, relative)

    actual = resolve_git_commit(root)
    if actual != expected:
        raise RuntimeError(
            f"worktree commit mismatch after restore: expected {expected}, found {actual}"
        )
    remaining = tuple(
        relative
        for relative in ignored
        if (target := root.joinpath(*PurePosixPath(relative).parts)).exists()
        or target.is_symlink()
    )
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
