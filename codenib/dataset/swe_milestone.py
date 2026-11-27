# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Load local SWE-Milestone specifications without exposing reference code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def _identity(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid milestone identity: {value!r}")
    return value


@dataclass(frozen=True)
class SWEMilestoneCase:
    """A specification and dependencies, independent of grading artifacts."""

    instance_id: str
    repo: str
    title: str
    problem_statement: str
    parent_milestones: tuple[str, ...]

    def prompt_record(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "title": self.title,
            "problem_statement": self.problem_statement,
            "parent_milestones": list(self.parent_milestones),
        }


def _reject_cycles(rows: dict[str, dict]) -> None:
    """A dependency cycle has no valid execution order."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished

    def visit(mid: str) -> None:
        state[mid] = 1
        parents = rows[mid].get("parent_milestones", [])
        for parent in parents if isinstance(parents, list) else []:
            if parent not in rows:
                continue  # reported as an invalid dependency by the caller
            if state.get(parent) == 1:
                raise ValueError(f"dependency cycle through: {parent}")
            if parent not in state:
                visit(parent)
        state[mid] = 2

    for mid in rows:
        if mid not in state:
            visit(mid)


def load_swe_milestone_cases(
    workspace: str | Path, *, instance_ids: Sequence[str] = ()
) -> tuple[SWEMilestoneCase, ...]:
    """Read metadata.json and selected srs/*_SRS.md files, never solutions."""
    root = Path(workspace).resolve()
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    repo = metadata.get("repo_name")
    if not isinstance(repo, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
    ):
        raise ValueError("metadata.repo_name must be an owner/repository slug")
    rows = metadata.get("milestones")
    if not isinstance(rows, list) or not rows:
        raise ValueError("metadata.milestones must be a nonempty list")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("milestone rows must be objects")
        mid = _identity(row.get("id"))
        if mid in indexed:
            raise ValueError(f"duplicate milestone: {mid}")
        indexed[mid] = row
    _reject_cycles(indexed)
    requested = {_identity(mid) for mid in instance_ids}
    if requested - indexed.keys():
        raise ValueError(f"unknown milestones: {sorted(requested - indexed.keys())}")
    cases = []
    for mid, row in indexed.items():
        if requested and mid not in requested:
            continue
        parents = row.get("parent_milestones", [])
        if not isinstance(parents, list):
            raise ValueError(f"invalid dependencies: {mid}")
        dependencies = tuple(_identity(parent) for parent in parents)
        if set(dependencies) - indexed.keys() or mid in dependencies:
            raise ValueError(f"invalid dependencies: {mid}")
        specification = (root / "srs" / f"{mid}_SRS.md").resolve()
        if not specification.is_relative_to(root):
            raise ValueError("specification escapes dataset workspace")
        statement = specification.read_text(encoding="utf-8")
        if not statement.strip():
            raise ValueError(f"empty milestone specification: {mid}")
        cases.append(
            SWEMilestoneCase(
                mid, repo, str(row.get("title", "")), statement, dependencies
            )
        )
    return tuple(cases)
