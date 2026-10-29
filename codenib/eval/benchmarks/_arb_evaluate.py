# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stdlib-only subprocess bridge; the pinned ARB checkout owns every metric."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def corpus_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def score_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from agent_retrieval_bench.baseline import (
        query_has_leakage,
        query_text_for_eval,
        recall_at,
        reciprocal_rank,
        target_gold_files,
    )
    from agent_retrieval_bench.bcy_curve import (
        DEFAULT_BUDGETS,
        CorpusFileCache,
        evaluate_sample,
    )

    results = []
    for job in jobs:
        sample = job["sample"]
        row = {
            "sample_id": sample["id"],
            "task_type": sample["task_type"],
            "repo": sample["repo"],
            "base_commit": sample["base_commit"],
            "status": "error",
        }
        try:
            if job.get("error"):
                raise ValueError(job["error"])
            prediction = job["prediction"]
            if prediction is None:
                raise ValueError("missing prediction")
            for key in ("repo", "base_commit"):
                if prediction.get(key) != sample[key]:
                    raise ValueError(f"prediction {key} mismatch")
            if prediction.get("status") != "ok":
                raise ValueError(prediction.get("error") or "retrieval failed")
            if query_has_leakage(sample, query_text_for_eval(sample)):
                raise ValueError("official query leakage check failed")
            ranked = prediction.get("ranked_files")
            if not isinstance(ranked, list) or not all(
                isinstance(path, str) and path for path in ranked
            ):
                raise ValueError("ranked_files must be a list of file paths")
            # Accept chunk-derived repeated paths, preserving first occurrence.
            ranked = list(dict.fromkeys(ranked))
            corpus_path = Path(job["corpus_path"])
            if corpus_digest(corpus_path) != job["corpus_sha256"]:
                raise ValueError("corpus changed before official scoring")
            cache = CorpusFileCache(
                {(sample["repo"], sample["base_commit"]): corpus_path}
            )
            if job.get("restrict_to_candidates"):
                # Full-source rankings score only released candidate files.
                kept = [
                    path
                    for path in ranked
                    if cache.file_text(sample["repo"], sample["base_commit"], path)
                    is not None
                ]
                row["removed_non_candidate_paths"] = len(ranked) - len(kept)
                ranked = kept
            gold = target_gold_files(sample)
            if not gold:
                raise ValueError("positive task has no official gold files")
            for path in set(ranked) | set(gold):
                if cache.file_text(sample["repo"], sample["base_commit"], path) is None:
                    raise ValueError(f"file missing from canonical corpus: {path}")
            packed = evaluate_sample(
                {**row, "top_files": ranked[:20]}, gold, cache, DEFAULT_BUDGETS
            )["packed"]
            if corpus_digest(corpus_path) != job["corpus_sha256"]:
                raise ValueError("corpus changed during official scoring")
            metrics = {
                f"Recall@{k}": recall_at(set(gold), ranked, k) for k in (5, 10, 20)
            }
            metrics["MRR"] = reciprocal_rank(set(gold), ranked)
            metrics.update(
                {f"BCY@{budget}": packed[budget]["bcy"] for budget in DEFAULT_BUDGETS}
            )
            row.update(
                status="ok",
                metrics=metrics,
                gold_files=gold,
                top_files=ranked[:20],
                ranking_depth=len(ranked),
                packed=packed,
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        results.append(row)
    return results


if __name__ == "__main__":
    sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
    json.dump(score_jobs(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
