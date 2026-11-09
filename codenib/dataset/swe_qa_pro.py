# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""SWE-QA-Pro tasks and deterministic release selection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

PRO_DATASET = "TIGER-Lab/SWE-QA-Pro-Bench"
PRO_DATASET_REVISION = "596892dac60b6f500f01a7dc2becb9f66593b7b7"


@dataclass(frozen=True)
class SWEQAProCase:
    instance_id: str
    repo: str
    commit_id: str
    cluster: dict[str, str]
    qa_type: dict[str, str]
    question: str
    reference_answer: str

    @property
    def repo_name(self):
        return self.repo.rsplit("/", 1)[-1]

    def task_record(self):
        """Export runtime inputs and metadata without the reference answer."""
        return {
            key: getattr(self, key)
            for key in (
                "instance_id",
                "repo",
                "commit_id",
                "cluster",
                "qa_type",
                "question",
            )
        }


def load_swe_qa_pro_cases(path=None, *, repos=(), question_ids=(), limit=None):
    """Load pinned test.jsonl or a local copy, preserving original row identities.

    Preserve the released cluster and qa_type metadata objects.
    """
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    if path is None:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(
            PRO_DATASET,
            "data/test.jsonl",
            repo_type="dataset",
            revision=PRO_DATASET_REVISION,
        )
    cases = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        repo, commit = row["repo"], row["commit_id"]
        if not isinstance(repo, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo
        ):
            raise ValueError(f"row {number}: invalid repository slug")
        if any(part in {".", ".."} for part in repo.split("/")):
            raise ValueError(f"row {number}: invalid repository slug")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"row {number}: invalid commit_id")
        if any(
            not isinstance(row.get(k), str) or not row[k].strip()
            for k in ("question", "answer")
        ):
            raise ValueError(f"row {number}: missing question/answer")
        for field, keys in (
            ("cluster", {"id", "name"}),
            ("qa_type", {"class_name", "sub_class_name"}),
        ):
            value = row.get(field)
            if not isinstance(value, dict) or not keys <= value.keys():
                raise ValueError(f"row {number}: invalid {field} metadata")
        cases.append(
            SWEQAProCase(
                f"{repo.rsplit('/', 1)[-1]}:{number}",
                repo,
                commit,
                dict(row["cluster"]),
                dict(row["qa_type"]),
                row["question"],
                row["answer"],
            )
        )
    requested = set(repos)
    known = {name for c in cases for name in (c.repo, c.repo_name)}
    if requested - known:
        raise ValueError(f"unknown repositories: {sorted(requested - known)}")
    cases = [c for c in cases if not requested or requested & {c.repo, c.repo_name}]
    selected = set(question_ids)
    if selected - {c.instance_id for c in cases}:
        raise ValueError("unknown question IDs in selected repositories")
    cases = [c for c in cases if not selected or c.instance_id in selected]
    cases = cases[:limit] if limit is not None else cases
    if not cases:
        raise ValueError("empty SWE-QA-Pro selection")
    return tuple(cases)
