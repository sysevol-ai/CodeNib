# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Churn hotspot signal: most-changed files over a git window.

Moved from codeminer.guardian.signals (flat module) into the signals/
sub-package.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Set

from ...log_utils import get_logger

logger = get_logger(__name__)

# Code-file extensions worth surfacing as hotspots (mirrors scripts/index_repo.py).
_CODE_EXTENSIONS: Set[str] = {
    ".py",
    ".pyi",
    ".go",
    ".rs",
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".h",
    ".hpp",
    ".hxx",
}


@dataclass
class Hotspot:
    """A file ranked by how often it changed in the churn window."""

    path: str
    commit_count: int


def churn_hotspots(
    repo_path: str,
    *,
    since: str = "90 days ago",
    top_n: int = 10,
    extensions: Optional[Set[str]] = None,
) -> List[Hotspot]:
    """Return the most-changed code files since ``since``, highest churn first.

    Churn = number of commits in the window that touched the file. Only files
    that still exist on disk and carry a code extension are returned, so deleted
    or renamed-away paths don't pollute the ranking.

    Args:
        repo_path: Repository root (a git checkout).
        since: A git ``--since`` expression (e.g. ``"90 days ago"``, ``"2026-01-01"``).
        top_n: Maximum number of hotspots to return.
        extensions: Override the set of code extensions to consider.

    Returns:
        Up to ``top_n`` :class:`Hotspot` records sorted by descending commit
        count, then by path for stable ordering. Empty if not a git repo.
    """
    exts = extensions if extensions is not None else _CODE_EXTENSIONS
    try:
        # One file per line, commits separated by blank lines. Each appearance of
        # a path == one commit that touched it, so counting lines counts commits.
        output = subprocess.run(
            ["git", "log", f"--since={since}", "--name-only", "--format="],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("churn_hotspots: git log failed in %s: %s", repo_path, exc)
        return []

    counts: dict[str, int] = {}
    for line in output.splitlines():
        rel = line.strip()
        if not rel:
            continue
        if os.path.splitext(rel)[1].lower() not in exts:
            continue
        if not os.path.isfile(os.path.join(repo_path, rel)):
            continue
        counts[rel] = counts.get(rel, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [Hotspot(path=path, commit_count=count) for path, count in ranked[:top_n]]
