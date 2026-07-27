# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared repository traversal policy for user-facing index builders."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator

REPOSITORY_FILTER_POLICY_VERSION = 2

DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".codenib_cache",
        ".env",
        ".git",
        ".hg",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "site",
        "target",
        "third_party",
        "vendor",
        "venv",
    }
)


def default_exclude_patterns() -> list[str]:
    """Return backend-neutral glob patterns for generated or vendored trees."""

    return [
        pattern
        for name in sorted(DEFAULT_IGNORED_DIRS)
        for pattern in (f"{name}/**", f"**/{name}/**")
    ]


def repository_path_is_visible(path: str | Path) -> bool:
    """Whether a repository-relative path survives the shared directory policy."""

    parts = Path(path).parts
    return not any(
        part in DEFAULT_IGNORED_DIRS or part.startswith(".codenib") for part in parts
    )


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def walk_repository_files(
    root: str | Path,
    *,
    exclude_roots: Iterable[str | Path] = (),
) -> Iterator[Path]:
    """Yield files under *root* after applying the shared directory policy."""

    root_path = Path(root).expanduser().resolve()
    excluded = tuple(Path(path).expanduser().resolve() for path in exclude_roots)
    for current_root, dirs, files in os.walk(root_path):
        dirs[:] = sorted(
            directory
            for directory in dirs
            if directory not in DEFAULT_IGNORED_DIRS
            and not directory.startswith(".codenib")
            and not _is_within(Path(current_root) / directory, excluded)
        )
        current = Path(current_root)
        for filename in sorted(files):
            path = current / filename
            if not _is_within(path, excluded):
                yield path


def count_repository_files(root: str | Path) -> int:
    """Count files visible to the default repository traversal policy."""

    return sum(1 for _ in walk_repository_files(root))


__all__ = [
    "DEFAULT_IGNORED_DIRS",
    "REPOSITORY_FILTER_POLICY_VERSION",
    "count_repository_files",
    "default_exclude_patterns",
    "repository_path_is_visible",
    "walk_repository_files",
]
