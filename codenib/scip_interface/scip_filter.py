# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free path filtering shared by SCIP graph and query routes.

The full-graph indexer historically owns these semantics.  Keeping them in a
small module lets a graph-free query consumer prove that its complete native
fact surface already satisfies the same filter without importing ``igraph``.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable, Mapping

from ..repository_filters import repository_path_is_visible


def normalize_scip_path(path: str | Path | None) -> str | None:
    """Normalize an indexer path exactly as the legacy graph filter does."""

    if path is None:
        return None
    normalized = Path(str(path).strip().replace("\\", "/")).as_posix().strip("/")
    return normalized or None


def definition_path(attributes: Mapping[str, object]) -> str | None:
    """Return a symbol definition path encoded in ``unified_name`` if present."""

    unified_name = attributes.get("unified_name")
    if isinstance(unified_name, str) and ":" in unified_name:
        return unified_name.split(":", 1)[0]
    return None


def matches_exclude_patterns(path: str, patterns: Iterable[str]) -> bool:
    """Apply the SCIP route's established glob and ``/**`` prefix rules."""

    for pattern in patterns:
        normalized = normalize_scip_path(pattern)
        if normalized is None:
            continue
        if fnmatch.fnmatch(path, normalized):
            return True
        if normalized.endswith("/**"):
            prefix = normalized[:-3]
            if path == prefix or path.startswith(f"{prefix}/"):
                return True
    return False


def scip_path_is_allowed(
    path: str | Path,
    *,
    target_dir: str | Path | None = None,
    exclude_patterns: Iterable[str] = (),
    allow_target_ancestor: bool = False,
    enforce_repository_policy: bool = False,
) -> bool:
    """Return whether *path* survives the SCIP route filters.

    ``enforce_repository_policy`` is opt-in so extracting this helper does not
    change direct/full-graph decoder behavior.  Manifest-backed consumers turn
    it on because their source fingerprint is defined by repository policy v3.
    """

    relative = normalize_scip_path(path)
    if not relative:
        return False
    target = normalize_scip_path(target_dir)
    if target:
        in_target = relative == target or relative.startswith(f"{target}/")
        is_target_ancestor = allow_target_ancestor and target.startswith(f"{relative}/")
        if not in_target and not is_target_ancestor:
            return False
    if enforce_repository_policy and not repository_path_is_visible(relative):
        return False
    return not matches_exclude_patterns(relative, exclude_patterns)


__all__ = [
    "definition_path",
    "matches_exclude_patterns",
    "normalize_scip_path",
    "scip_path_is_allowed",
]
