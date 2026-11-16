# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Released CORE-Bench corpora, queries and task selection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CORE_DATASET = "zhangfw123/CORE-Bench"
CORE_DATASET_REVISION = "23da5d97a8a4495d4bd6d5616aa86d5a12a60699"


def _rows(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


@dataclass(frozen=True)
class CoreQuery:
    query_id: str
    text: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class CoreCorpus:
    document_ids: tuple[str, ...]
    texts: tuple[str, ...]
    queries: tuple[CoreQuery, ...]


def core_repository_directories(directory: str | Path) -> tuple[Path, ...]:
    """Select one repository, a released task group, or a complete level."""
    root = Path(directory).resolve()
    paths = tuple(sorted(path.parent for path in root.rglob("corpus.jsonl")))
    if not paths:
        raise ValueError("no released CORE repository corpora found")
    return paths


def load_core_corpus(
    directory: str | Path, *, query_ids=(), limit: int | None = None
) -> CoreCorpus:
    """Read one released repository corpus without loading its gold labels.

    Pass a directory containing corpus.jsonl and queries.jsonl. Candidate sets
    identify the snapshot eligible for each query; they are not relevance labels.
    """
    directory = Path(directory)
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    documents = list(_rows(directory / "corpus.jsonl"))
    ids = tuple(row["_id"] for row in documents)
    texts = tuple(row["text"] for row in documents)
    if not ids or any(not isinstance(i, str) or not i for i in ids):
        raise ValueError("corpus requires nonempty string document IDs")
    if len(set(ids)) != len(ids) or any(not isinstance(t, str) for t in texts):
        raise ValueError("invalid corpus IDs or text")
    known, queries, seen = set(ids), [], set()
    for row in _rows(directory / "queries.jsonl"):
        qid, text, allowed = row["_id"], row["text"], row.get("filtered_corpus_id")
        if not isinstance(qid, str) or not qid or qid in seen:
            raise ValueError("invalid or duplicate query ID")
        if not isinstance(text, str) or not isinstance(allowed, list):
            raise ValueError(f"{qid}: invalid text or missing candidate allowlist")
        if any(not isinstance(i, str) for i in allowed):
            raise ValueError(f"{qid}: candidate IDs must be strings")
        if len(set(allowed)) != len(allowed) or not set(allowed) <= known:
            raise ValueError(f"{qid}: invalid candidate allowlist")
        seen.add(qid)
        queries.append(CoreQuery(qid, text, tuple(allowed)))
    selected = set(query_ids)
    if selected - seen:
        raise ValueError(f"unknown queries: {sorted(selected - seen)}")
    queries = [q for q in queries if not selected or q.query_id in selected]
    queries = queries[:limit] if limit is not None else queries
    if not queries:
        raise ValueError("empty query selection")
    return CoreCorpus(ids, texts, tuple(queries))


def load_core_qrels(directory: str | Path) -> dict[str, set[str]]:
    """Load positive annotations separately, for scoring only."""
    gold: dict[str, set[str]] = {}
    for row in _rows(Path(directory) / "qrels.jsonl"):
        if row["score"] > 0:
            gold.setdefault(row["query_id"], set()).add(row["corpus_id"])
    return gold
