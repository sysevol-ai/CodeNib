# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pinned ContextBench task loading, selection, and annotation validation.

The data layer does not execute agents or score predictions. Curated source
ranges use 1-based inclusive lines and stay separate from the issue prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

CONTEXTBENCH_DATASET = "Contextbench/ContextBench"
CONTEXTBENCH_DATASET_REVISION = "c2855792b006af41c67202d33883fb9d46362853"
CONTEXTBENCH_CONFIGS = ("default", "contextbench_verified")
_SAFE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def _validated_gold_path(value: str) -> str:
    """Validate the source portion but preserve the annotation's original path.

    Upstream normalization is not idempotent for container-prefixed dotfiles.
    Keeping the original spelling lets the official scorer apply it once.
    """
    path = value.replace("\\", "/")
    if path.startswith("/testbed/"):
        path = path[len("/testbed/") :]
    elif path.startswith("/workspace/"):
        path = path[len("/workspace/") :]
        path = path.split("/", 1)[-1]
    elif path.startswith("/"):
        path = path.lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    relative_source_path(path)
    return value


def relative_source_path(value: str) -> str:
    """Validate a repository-relative source identity without suffix guessing."""
    path = PurePosixPath(value.replace("\\", "/"))
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or ":" in path.parts[0]
        or "\x00" in value
    ):
        raise ValueError(f"invalid repository-relative path: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class ContextBenchCase:
    """An official task, keeping the curated and original identities distinct."""

    instance_id: str
    original_inst_id: str
    repo: str
    repo_url: str
    base_commit: str
    language: str
    source: str
    problem_statement: str
    gold_context: tuple[dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> ContextBenchCase:
        instance_id = str(row.get("instance_id") or "")
        if not _SAFE_ID.fullmatch(instance_id):
            raise ValueError(f"invalid ContextBench instance_id: {instance_id!r}")
        repo = str(row.get("repo") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError(f"invalid ContextBench repo: {repo!r}")
        commit = str(row.get("base_commit") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"invalid base_commit for {instance_id}")
        raw_gold = row.get("gold_context")
        if isinstance(raw_gold, str):
            raw_gold = json.loads(raw_gold)
        if not isinstance(raw_gold, list):
            raise ValueError(f"gold_context must be a list for {instance_id}")
        gold = []
        for span in raw_gold:
            if not isinstance(span, Mapping) or not isinstance(span.get("file"), str):
                raise ValueError(f"invalid gold span for {instance_id}: {span!r}")
            start, end = span.get("start_line"), span.get("end_line")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                raise ValueError(f"invalid gold span for {instance_id}: {span!r}")
            gold.append(
                {
                    "file": _validated_gold_path(span["file"]),
                    "start_line": start,
                    "end_line": end,
                }
            )
        # The dataset's curated repo_url can differ from its source repo slug.
        url = str(row.get("repo_url") or f"https://github.com/{repo}.git")
        if not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?", url
        ):
            raise ValueError(f"unsupported ContextBench repository URL: {url!r}")
        return cls(
            instance_id=instance_id,
            original_inst_id=str(row.get("original_inst_id") or ""),
            repo=repo,
            repo_url=url,
            base_commit=commit,
            language=str(row.get("language") or "").lower(),
            source=str(row.get("source") or ""),
            # Missing questions remain cases and become explicit run failures.
            problem_statement=str(row.get("problem_statement") or ""),
            gold_context=tuple(gold),
        )

    def to_record(self) -> dict[str, Any]:
        return {**vars(self), "gold_context": list(self.gold_context)}


def load_contextbench_cases(
    path: str | Path | None = None,
    *,
    config: str = "default",
    revision: str = CONTEXTBENCH_DATASET_REVISION,
    instance_ids: Sequence[str] = (),
    languages: Sequence[str] = (),
    sources: Sequence[str] = (),
    limit: int | None = None,
) -> tuple[ContextBenchCase, ...]:
    """Load HF's published train split or a local JSON/JSONL/Parquet table."""
    if config not in CONTEXTBENCH_CONFIGS:
        raise ValueError(f"unknown ContextBench config: {config}")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    if path is None:
        from datasets import load_dataset

        rows = load_dataset(
            CONTEXTBENCH_DATASET, config, revision=revision, split="train"
        )
    else:
        path = Path(path)
        if path.suffix == ".parquet":
            import pyarrow.parquet as pq

            rows = pq.read_table(path).to_pylist()
        elif path.suffix == ".jsonl":
            rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        else:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(rows, dict):
                rows = [rows]
    cases = [ContextBenchCase.from_mapping(row) for row in rows]
    aliases: dict[str, ContextBenchCase] = {}
    for case in cases:
        for key in {case.instance_id, case.original_inst_id} - {""}:
            if key in aliases:
                raise ValueError(f"duplicate ContextBench identity: {key}")
            aliases[key] = case
    unknown = set(instance_ids) - aliases.keys()
    if unknown:
        raise ValueError(f"unknown ContextBench instances: {sorted(unknown)}")
    requested = {aliases[key].instance_id for key in instance_ids}
    selected = tuple(
        case
        for case in cases
        if (not requested or case.instance_id in requested)
        and (not languages or case.language in languages)
        and (not sources or case.source.lower() in {s.lower() for s in sources})
    )
    if not selected:
        raise ValueError("ContextBench selection is empty")
    return selected[:limit] if limit is not None else selected


def cases_digest(cases: Sequence[ContextBenchCase]) -> str:
    """Hash exactly the ordered task inputs used by this run."""
    raw = json.dumps([c.to_record() for c in cases], sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()
