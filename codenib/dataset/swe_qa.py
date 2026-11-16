# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""SWE-QA release loading and deterministic task selection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SWE_QA_DATA_REVISION = "c13deac7a0d99b0ca2e593e004c4739475785b08"


@dataclass(frozen=True)
class SWEQACase:
    instance_id: str
    repo: str
    base_commit: str
    question: str
    reference_answer: str

    @property
    def repo_name(self):
        return self.repo.rsplit("/", 1)[-1]


def load_swe_qa_cases(upstream: str | Path, *, repos=(), question_ids=(), limit=None):
    """Read Benchmark/*.jsonl and repo_commit.txt from the released checkout.

    Upstream rows have question/answer but no question ID. IDs use the repository
    basename plus its original 1-based JSONL line, before any filtering.
    """
    root = Path(upstream)
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    commits = {}
    for line in (root / "repo_commit.txt").read_text().splitlines():
        if not line.strip():
            continue
        url, commit = line.split()
        match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?", url)
        if not match or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
            raise ValueError("invalid repository/commit in SWE-QA release")
        repo = match[1]
        name = repo.rsplit("/", 1)[-1]
        if name in commits:
            raise ValueError(f"duplicate repository basename: {name}")
        commits[name] = (repo, commit)
    requested = set(repos)
    if requested - commits.keys():
        raise ValueError(f"unknown repositories: {sorted(requested - commits.keys())}")
    cases = []
    for path in sorted((root / "Benchmark").glob("*.jsonl")):
        if requested and path.stem not in requested:
            continue
        repo, commit = commits[path.stem]
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if any(
                not isinstance(row.get(k), str) or not row[k].strip()
                for k in ("question", "answer")
            ):
                raise ValueError(f"{path.name}:{number}: missing question/answer")
            cases.append(
                SWEQACase(
                    f"{path.stem}:{number}",
                    repo,
                    commit,
                    row["question"],
                    row["answer"],
                )
            )
    selected = set(question_ids)
    unknown = selected - {case.instance_id for case in cases}
    if unknown:
        raise ValueError(f"unknown question IDs: {sorted(unknown)}")
    cases = [c for c in cases if not selected or c.instance_id in selected]
    cases = cases[:limit] if limit is not None else cases
    if not cases:
        raise ValueError("empty SWE-QA selection")
    return tuple(cases)
