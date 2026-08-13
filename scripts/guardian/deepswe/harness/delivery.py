# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Filesystem synchronization helpers for Guardian report delivery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CompletedReport:
    commit: str
    report: str
    identifiers: tuple[str, ...]
    completed_at_ns: int


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


def completed_report(exchange_root: Path, commits: set[str]) -> CompletedReport | None:
    """Load the newest completed commit-addressed review without using ``latest``."""

    reports = []
    for commit in commits:
        response = exchange_root / "responses" / commit
        status_path = response / "status.json"
        report_path = response / "findings.md"
        findings_path = response / "findings.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            report = report_path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(status, dict)
            or status.get("running", False)
            or status.get("review_performed") is not True
            or str(status.get("commit") or "").strip() != commit
        ):
            continue
        identifiers = []
        try:
            payload = json.loads(findings_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                for key in ("findings", "uncertain_specifications"):
                    rows = payload.get(key, [])
                    if isinstance(rows, list):
                        identifiers.extend(
                            str(row.get("specification_id") or "").strip()
                            for row in rows
                            if isinstance(row, dict)
                        )
        except (OSError, json.JSONDecodeError):
            pass
        reports.append(
            CompletedReport(
                commit=commit,
                report=report,
                identifiers=tuple(value for value in identifiers if value),
                completed_at_ns=status_path.stat().st_mtime_ns,
            )
        )
    return max(reports, key=lambda item: item.completed_at_ns, default=None)


__all__ = [
    "CompletedReport",
    "completed_report",
    "completed_responses",
    "review_request_commits",
]
