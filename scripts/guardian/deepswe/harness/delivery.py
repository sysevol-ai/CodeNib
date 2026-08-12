# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Filesystem synchronization helpers for Guardian report delivery."""

from __future__ import annotations

import json
from pathlib import Path


def review_request_commits(exchange_root: Path) -> set[str]:
    """Return commit-addressed requests that have been atomically published."""

    requests = exchange_root / "requests"
    if not requests.is_dir():
        return set()
    return {path.stem for path in requests.glob("*.json") if path.is_file()}


def completed_responses(exchange_root: Path, commits: set[str]) -> set[str]:
    """Return requested commits whose controller response reached a terminal state."""

    completed = set()
    for commit in commits:
        status_path = exchange_root / "responses" / commit / "status.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(status, dict) and not status.get("running", False):
            completed.add(commit)
    return completed


__all__ = ["completed_responses", "review_request_commits"]
