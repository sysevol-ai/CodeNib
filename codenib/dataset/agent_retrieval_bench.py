# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Released ARB inputs. This module performs no retrieval or scoring."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

ARB_DATASET = "eyuansu71/agent_retrieval_bench"
ARB_DATASET_REVISION = "5901e1ee3aff048290db72edf9c63bc498b79ea3"
ARB_RELEASES = {
    "v2_code2test": "code2test",
    "v2_comment2context": "comment2context",
    "v2_trace2code": "trace2code",
    "v2_edit2ripple": "edit2ripple",
}
ARB_RELEASE_COUNTS = {
    "v2_code2test": 106,
    "v2_comment2context": 80,
    "v2_trace2code": 101,
    "v2_edit2ripple": 58,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: expected a JSON object")
            yield row


def repository_path(value: Any) -> str:
    """Require canonical relative POSIX paths, including extensionless files."""
    if not isinstance(value, str) or not value:
        raise ValueError("expected a nonempty repository path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(char in value for char in ("\\", "\x00", ":"))
    ):
        raise ValueError(f"unsafe repository path: {value!r}")
    return value


def contained_path(data_root: Path, relative: str) -> Path:
    repository_path(relative)
    root = data_root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path escapes data root: {relative}")
    return path


@dataclass(frozen=True)
class ARBCase:
    release: str
    sample: dict[str, Any]

    @property
    def sample_id(self) -> str:
        return self.sample["id"]

    @property
    def query_text(self) -> str:
        # Match upstream query_text_for_eval; annotations never enter this text.
        return json.dumps(self.sample["query"], ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_record(cls, release: str, sample: dict[str, Any]) -> ARBCase:
        if release not in ARB_RELEASES:
            raise ValueError(f"unsupported positive release: {release}")
        if not isinstance(sample.get("id"), str) or not sample["id"]:
            raise ValueError("sample is missing id")
        repo = repository_path(sample.get("repo"))
        if len(repo.split("/")) != 2:
            raise ValueError(f"expected owner/repo: {repo}")
        commit = sample.get("base_commit", "")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("base_commit must be a full lowercase Git SHA")
        if sample.get("task_type") != ARB_RELEASES[release]:
            raise ValueError("task_type does not match release")
        if not isinstance(sample.get("query"), dict) or not isinstance(
            sample.get("gold"), dict
        ):
            raise ValueError("query and gold must be objects")
        if sample["gold"].get("no_gold"):
            raise ValueError("abstention/selective samples are not supported")
        candidate = sample.get("candidate_corpus") or {}
        if (
            candidate.get("type") != "repo_at_base_commit"
            or candidate.get("base_commit") != commit
        ):
            raise ValueError("candidate corpus does not match base_commit")
        return cls(release, sample)


def load_cases(
    data_root: Path,
    releases: Iterable[str] = ARB_RELEASES,
    *,
    sample_ids: Iterable[str] = (),
    limit_per_task: int | None = None,
) -> list[ARBCase]:
    """Select in release/id order, before inspecting retrieval results or gold."""
    releases = list(releases)
    if not releases or len(set(releases)) != len(releases):
        raise ValueError("provide nonempty, distinct releases")
    if limit_per_task is not None and limit_per_task <= 0:
        raise ValueError("limit_per_task must be positive")
    selected = set(sample_ids)
    cases: list[ARBCase] = []
    seen: set[str] = set()
    found: set[str] = set()
    for release in releases:
        if release not in ARB_RELEASES:
            raise ValueError(f"unsupported positive release: {release}")
        rows = read_jsonl(
            contained_path(data_root, f"benchmark/{release}/samples.jsonl")
        )
        release_cases = [ARBCase.from_record(release, row) for row in rows]
        for case in release_cases:
            if case.sample_id in seen:
                raise ValueError(f"duplicate sample id: {case.sample_id}")
            seen.add(case.sample_id)
        expected = ARB_RELEASE_COUNTS[release]
        if len(release_cases) != expected:
            raise ValueError(
                f"incomplete or unexpected release {release}: "
                f"expected {expected} samples, got {len(release_cases)}"
            )
        kept = 0
        for case in sorted(release_cases, key=lambda item: item.sample_id):
            if selected and case.sample_id not in selected:
                continue
            found.add(case.sample_id)
            if limit_per_task is None or kept < limit_per_task:
                cases.append(case)
                kept += 1
    if selected - found:
        raise ValueError(f"unknown sample ids: {sorted(selected - found)}")
    if not cases:
        raise ValueError("empty case selection")
    return cases


def corpus_manifest(data_root: Path, release: str) -> dict[tuple[str, str], Path]:
    if release not in ARB_RELEASES:
        raise ValueError(f"unsupported positive release: {release}")
    manifest = contained_path(data_root, f"corpus/{release}/corpus_manifest.jsonl")
    result = {}
    for row in read_jsonl(manifest):
        key = (row["repo"], row["base_commit"])
        if key in result:
            raise ValueError(f"duplicate corpus snapshot: {key}")
        if row.get("status", "ok") != "ok":
            continue
        relative = row["chunks_path"]
        # Released manifests are rooted at 'data/', not the caller's cwd.
        if relative.startswith("data/"):
            relative = relative[len("data/") :]
        if not relative.startswith(f"corpus/{release}/"):
            raise ValueError("corpus reference belongs to a different release")
        result[key] = contained_path(data_root, relative)
    return result


def load_corpus(path: Path, repo: str, commit: str) -> list[dict[str, Any]]:
    """Preserve official chunks and require one canonical file row per path."""
    rows = list(read_jsonl(path))
    chunk_ids: set[str] = set()
    files: set[str] = set()
    paths: set[str] = set()
    for row in rows:
        if row.get("repo") != repo or row.get("base_commit") != commit:
            raise ValueError("corpus repo/base_commit mismatch")
        file_path = repository_path(row.get("path"))
        paths.add(file_path)
        chunk_id = row.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in chunk_ids:
            raise ValueError("missing or duplicate chunk_id")
        chunk_ids.add(chunk_id)
        if row.get("kind") not in {"file", "symbol"} or not isinstance(
            row.get("text"), str
        ):
            raise ValueError("invalid corpus kind/text")
        start, end = row.get("start_line"), row.get("end_line")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("corpus lines must be one-based inclusive ranges")
        if row["kind"] == "file":
            if file_path in files:
                raise ValueError(f"duplicate canonical file: {file_path}")
            files.add(file_path)
    if not rows or paths != files:
        raise ValueError("empty corpus or missing canonical kind=file rows")
    return rows
