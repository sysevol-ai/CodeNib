# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Code walkthroughs prebuilt for a Wiki.

A walkthrough follows one question through the code, one function per step:
from the call a user writes to the function that answers the question, along
calls the index recorded. ``scripts/walkthrough/`` builds them offline, one
file per repository at ``<data_dir>/walkthroughs/<repo_id>.json``.

Every step cites line numbers, so a file is served only for the commit it was
built from; the caller checks that against the indexed commit.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

WALKTHROUGH_VERSION = 1
_MAX_BYTES = 4 * 1024 * 1024
_REPO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# What the reader needs; the build log stays on disk.
_PUBLIC_KEYS = ("version", "repo", "areas", "links", "paths", "wiki", "walks", "source")


def walkthrough_path(data_dir: str | os.PathLike, repo_id: str) -> Path | None:
    """Where a repository's walkthroughs live, or None for an unsafe id."""

    if not _REPO_ID.fullmatch(repo_id) or ".." in repo_id:
        return None
    return Path(os.path.abspath(data_dir)) / "walkthroughs" / f"{repo_id}.json"


def _well_formed(data: object, repo_id: str) -> bool:
    if not isinstance(data, dict) or data.get("version") != WALKTHROUGH_VERSION:
        return False
    repo = data.get("repo")
    if not isinstance(repo, dict) or repo.get("id") != repo_id:
        return False
    if not isinstance(repo.get("commit"), str):
        return False
    walks = data.get("walks")
    if not isinstance(walks, list) or not walks:
        return False
    for walk in walks:
        if not isinstance(walk, dict) or not isinstance(walk.get("id"), str):
            return False
        if not isinstance(walk.get("steps"), list) or not walk["steps"]:
            return False
        if not isinstance(walk.get("stops"), list):
            return False
    return isinstance(data.get("source"), dict) and isinstance(data.get("wiki"), dict)


def load_walkthroughs(data_dir: str | os.PathLike, repo_id: str) -> dict | None:
    """Read and shape-check one repository's walkthroughs; None when absent or invalid."""

    path = walkthrough_path(data_dir, repo_id)
    if path is None:
        return None
    try:
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _well_formed(data, repo_id):
        return None
    return {key: data[key] for key in _PUBLIC_KEYS if key in data}
