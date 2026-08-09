# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic content identity for repository files visible to CodeNib."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ._contained_source import (
    _version_identity,
    resolved_repository_file,
    update_repository_file_hash,
)
from .repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    repository_path_is_visible,
    walk_repository_files,
)

SOURCE_FINGERPRINT_VERSION = 1


class RepositoryChangedError(RuntimeError):
    """Raised when repository contents change while they are being inspected."""


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    """Content identity and file count for one observed repository state."""

    value: str
    file_count: int


def _link_matches(
    path: Path,
    *,
    initial: os.stat_result,
    target: str,
) -> bool:
    try:
        observed = path.lstat()
        observed_target = os.readlink(path)
    except OSError:
        return False
    return _version_identity(observed) == _version_identity(initial) and (
        observed_target == target
    )


def fingerprint_repository(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> SourceFingerprint:
    """Hash the paths and contents exposed by the shared repository filter."""

    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ValueError(f"repository path is not a directory: {root_path}")

    hasher = hashlib.sha256()
    hasher.update(
        (
            f"codenib-source-fingerprint:{SOURCE_FINGERPRINT_VERSION}:"
            f"{REPOSITORY_FILTER_POLICY_VERSION}\0"
        ).encode("ascii")
    )
    file_count = 0

    for path in walk_repository_files(root_path, exclude_roots=exclude_roots):
        relative_text = path.relative_to(root_path).as_posix()
        relative = relative_text.encode("utf-8", errors="surrogateescape")
        try:
            initial_metadata = path.lstat()
        except OSError as exc:
            raise RepositoryChangedError(
                f"repository file disappeared while it was being inspected: {path}"
            ) from exc

        mode = initial_metadata.st_mode
        if stat.S_ISLNK(mode):
            try:
                raw_target = os.readlink(path)
                target = raw_target.encode("utf-8", errors="surrogateescape")
            except OSError as exc:
                raise RepositoryChangedError(
                    f"repository link changed while it was being inspected: {path}"
                ) from exc
            hasher.update(b"L\0")
            hasher.update(relative)
            hasher.update(b"\0")
            hasher.update(target)
            hasher.update(b"\0")
            try:
                with resolved_repository_file(
                    root_path,
                    relative_text,
                    expected_final_identity=_version_identity(initial_metadata),
                ) as binding:
                    if binding.is_regular:
                        hasher.update(b"C\0")
                        binding.update_hash(hasher)
            except (OSError, ValueError) as exc:
                raise RepositoryChangedError(
                    "repository link target could not be read consistently: " f"{path}"
                ) from exc
            if not _link_matches(path, initial=initial_metadata, target=raw_target):
                raise RepositoryChangedError(
                    f"repository link changed while it was being read: {path}"
                )
            if binding.is_regular:
                hasher.update(b"\0")
            file_count += 1
            continue

        if not stat.S_ISREG(mode):
            continue

        hasher.update(b"F\0")
        hasher.update(relative)
        hasher.update(b"\0")
        try:
            update_repository_file_hash(
                root_path,
                relative_text,
                hasher,
                expected_final_identity=_version_identity(initial_metadata),
            )
        except (OSError, ValueError) as exc:
            raise RepositoryChangedError(
                f"repository file could not be read consistently: {path}"
            ) from exc
        hasher.update(b"\0")
        file_count += 1

    return SourceFingerprint(
        value=f"sha256:{hasher.hexdigest()}",
        file_count=file_count,
    )


def repository_source_is_dirty(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> bool:
    """Return whether Git reports source-visible worktree changes.

    Non-Git repositories return ``True`` so callers conservatively avoid
    Git-based incremental update paths.
    """

    root_path = Path(root).expanduser().resolve()
    excluded = tuple(Path(path).expanduser().resolve() for path in exclude_roots)
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root_path),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except Exception:
        return True
    if result.returncode != 0:
        return True

    records = result.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status_code = record[:2]
        paths = [record[3:]]
        if b"R" in status_code or b"C" in status_code:
            if index < len(records) and records[index]:
                paths.append(records[index])
                index += 1

        for encoded in paths:
            relative = Path(os.fsdecode(encoded))
            absolute = root_path / relative
            if any(absolute == path or path in absolute.parents for path in excluded):
                continue
            if repository_path_is_visible(relative):
                return True
    return False


__all__ = [
    "RepositoryChangedError",
    "SOURCE_FINGERPRINT_VERSION",
    "SourceFingerprint",
    "fingerprint_repository",
    "repository_source_is_dirty",
]
