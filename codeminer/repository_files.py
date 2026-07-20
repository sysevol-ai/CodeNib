# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Repository file-selection primitives shared by index backends."""

from __future__ import annotations

import os
from pathlib import Path
from typing import AbstractSet, Iterable

DEFAULT_IGNORED_REPOSITORY_DIRS = frozenset(
    {
        ".env",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "target",
        "venv",
    }
)


def iter_repository_source_files(
    project_root: str | Path,
    *,
    extensions: AbstractSet[str],
    target_dir: str | None = None,
    ignored_dirs: AbstractSet[str] = DEFAULT_IGNORED_REPOSITORY_DIRS,
) -> Iterable[Path]:
    """Yield source paths relative to ``project_root`` in stable order.

    Generated dependency and build trees are pruned before traversal. This is
    important for prepared repositories, where installing a language server or
    project dependencies can create many untracked source-looking files.
    """

    root = Path(project_root).resolve()
    walk_root = (root / target_dir).resolve() if target_dir else root
    try:
        walk_root.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Target directory {walk_root} is outside {root}") from exc

    if not walk_root.is_dir():
        return

    for current_root, dir_names, file_names in os.walk(walk_root):
        dir_names[:] = sorted(name for name in dir_names if name not in ignored_dirs)
        current_path = Path(current_root)
        for file_name in sorted(file_names):
            path = current_path / file_name
            if path.suffix in extensions:
                yield path.relative_to(root)


__all__ = ["DEFAULT_IGNORED_REPOSITORY_DIRS", "iter_repository_source_files"]
