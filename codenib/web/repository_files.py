# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Safe reads from a repository checkout or an immutable Git tree."""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from typing import TYPE_CHECKING, Optional

from .._contained_source import read_repository_file

if TYPE_CHECKING:
    from ..source_fingerprint import RepositorySourceReader

_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_DEFAULT_SOURCE_LINES = 400
_MAX_SOURCE_LINES = 1_000
# These values can contain every tracked path in a large repository. Retain the
# current and previous immutable trees instead of scaling with window length.
_MAX_GIT_SNAPSHOT_CACHE_ENTRIES = 2


def valid_commit(commit: object) -> bool:
    return isinstance(commit, str) and _COMMIT_RE.fullmatch(commit) is not None


def has_git_metadata(repo_dir: str) -> bool:
    """Return whether *repo_dir* declares its own Git metadata boundary.

    A read-only prebuilt source snapshot intentionally has no ``.git`` entry.
    Broken or inaccessible metadata still counts as present so callers fail
    closed instead of treating a Git error as permission to use stale content.
    """

    return os.path.lexists(os.path.join(os.path.realpath(repo_dir), ".git"))


def safe_repo_relative_path(repo_dir: str, file_path: str) -> Optional[str]:
    """Return a repo-relative POSIX path without permitting an outside read.

    Absolute graph paths are accepted only when they resolve inside *repo_dir*.
    Relative paths reject parent traversal, and symlinks that escape the checkout
    are rejected before any live-file read. The returned lexical path also names
    the corresponding object inside a Git tree.
    """
    root = os.path.realpath(repo_dir)
    if os.path.isabs(file_path):
        candidate = os.path.realpath(file_path)
        try:
            if os.path.commonpath((root, candidate)) != root:
                return None
        except ValueError:
            return None
        relative = os.path.relpath(candidate, root)
    else:
        relative = os.path.normpath(file_path.replace("\\", os.sep))
        if (
            relative in ("", ".")
            or relative == ".."
            or relative.startswith(".." + os.sep)
        ):
            return None
        candidate = os.path.realpath(os.path.join(root, relative))
        try:
            if os.path.commonpath((root, candidate)) != root:
                return None
        except ValueError:
            return None
    return relative.replace(os.sep, "/")


def _git_blob(
    repo_dir: str,
    commit: str,
    relative: str,
    max_bytes: int,
    *,
    allow_truncated: bool = False,
) -> Optional[bytes]:
    if not valid_commit(commit):
        return None
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        process = subprocess.Popen(
            ["git", "-C", repo_dir, "show", f"{commit}:{relative}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        content = process.stdout.read(max_bytes + 1)
        process.stdout.close()
        truncated = len(content) > max_bytes
        if truncated and process.poll() is None:
            process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.SubprocessError):
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        return None
    if not truncated and process.returncode != 0:
        return None
    if truncated:
        return content[:max_bytes] if allow_truncated else None
    return content


def git_blob_head(
    repo_dir: str, commit: str, relative: str, max_bytes: int
) -> Optional[bytes]:
    """Read at most *max_bytes* from one blob in a selected Git tree."""
    return _git_blob(repo_dir, commit, relative, max_bytes, allow_truncated=True)


@lru_cache(maxsize=_MAX_GIT_SNAPSHOT_CACHE_ENTRIES)
def git_grep_paths(repo_dir: str, commit: str, pattern: str) -> frozenset[str]:
    """Return paths matching *pattern* at *commit* using one Git process."""
    if not valid_commit(commit):
        return frozenset()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                repo_dir,
                "grep",
                "-I",
                "-l",
                "-i",
                "-E",
                pattern,
                commit,
                "--",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode not in (0, 1):
        return frozenset()
    prefix = f"{commit}:"
    return frozenset(
        line[len(prefix) :] if line.startswith(prefix) else line.split(":", 1)[-1]
        for line in result.stdout.splitlines()
        if line
    )


@lru_cache(maxsize=_MAX_GIT_SNAPSHOT_CACHE_ENTRIES)
def git_tree_paths(repo_dir: str, commit: str) -> Optional[frozenset[str]]:
    """Repository paths at *commit*, or ``None`` when it is unavailable."""
    if not valid_commit(commit):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "ls-tree", "-r", "-z", "--name-only", commit],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return frozenset(
        path.decode("utf-8", errors="surrogateescape")
        for path in result.stdout.split(b"\0")
        if path
    )


def git_source_slice(
    repo_dir: str,
    commit: str,
    file_path: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> Optional[dict]:
    """Return the same 1-based source slice contract as the live wiki reader."""
    relative = safe_repo_relative_path(repo_dir, file_path)
    if relative is None:
        return None
    content = _git_blob(repo_dir, commit, relative, _MAX_SOURCE_BYTES)
    if content is None:
        return None
    return _source_slice(relative, content, start, end)


def _source_slice(
    relative: str,
    content: bytes,
    start: Optional[int],
    end: Optional[int],
) -> dict:
    all_lines = content.decode("utf-8", errors="replace").splitlines(keepends=True)
    first, last = _source_line_bounds(start, end)
    chunk = all_lines[first - 1 : last]
    return {
        "file": relative,
        "start_line": first,
        "end_line": first + len(chunk) - 1,
        "content": "".join(chunk),
    }


def _source_line_bounds(
    start: Optional[int],
    end: Optional[int],
) -> tuple[int, int]:
    first = max(1, int(start)) if start is not None else 1
    requested_last = (
        first + _DEFAULT_SOURCE_LINES - 1 if end is None else max(first, int(end))
    )
    return first, min(requested_last, first + _MAX_SOURCE_LINES - 1)


def bound_source_slice(
    source_reader: "RepositorySourceReader",
    file_path: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> Optional[dict]:
    """Read one current source slice through a borrowed authenticated reader."""

    relative = source_reader.captured_relative_path(file_path)
    if relative is None:
        return None
    first, last = _source_line_bounds(start, end)
    try:
        content = source_reader.read_line_range(
            relative,
            start_line=first,
            end_line=last,
            max_bytes=_MAX_SOURCE_BYTES,
        )
    except ValueError:
        return None
    chunk = content.decode("utf-8", errors="replace").splitlines(keepends=True)
    return {
        "file": relative,
        "start_line": first,
        "end_line": first + len(chunk) - 1,
        "content": "".join(chunk),
    }


def live_source_slice(
    repo_dir: str,
    file_path: str,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> Optional[dict]:
    """Read a bounded source slice from the current checkout safely."""
    relative = safe_repo_relative_path(repo_dir, file_path)
    if relative is None:
        return None
    try:
        content = read_repository_file(
            repo_dir,
            relative,
            max_bytes=_MAX_SOURCE_BYTES,
        )
    except ValueError:
        return None
    return _source_slice(relative, content, start, end)
