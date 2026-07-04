# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared answer-quality diagnostics for agent-compile sweep cells."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Dict, Optional, Tuple

from codeminer.eval.retrieval_eval import (
    compute_block_metrics,
    compute_path_alias_metrics,
    dedup_spans,
    diagnose_answer_spans,
)

ANSWER_DIAGNOSES = (
    "ok",
    "rank_gap",
    "path_alias_gap",
    "format_gap",
    "content_gap",
    "empty_answer",
)
ANSWER_DIAGNOSIS_SHORT = {
    "ok": "ok",
    "rank_gap": "rank",
    "path_alias_gap": "alias",
    "format_gap": "fmt",
    "content_gap": "content",
    "empty_answer": "empty",
}


def _answer_metric_at_k(metrics: Mapping[str, Any], k: int) -> Mapping[str, Any]:
    answer_blocks = metrics.get("answer_blocks") or {}
    if not isinstance(answer_blocks, Mapping):
        return {}
    value = answer_blocks.get(k, answer_blocks.get(str(k)))
    return value if isinstance(value, Mapping) else {}


def _recall_at_k(metrics: Mapping[str, Any], k: int) -> Optional[float]:
    value = _answer_metric_at_k(metrics, k).get("recall")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _diagnosis_k(metrics_k: Sequence[int]) -> int:
    ks = [int(k) for k in metrics_k or []]
    return 5 if 5 in ks else max(ks or [5])


def build_answer_diagnostic_fields(
    *,
    answer: str,
    answer_spans: Sequence[Mapping[str, Any]],
    gt_blocks: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    metrics_k: Sequence[int],
) -> Tuple[list[Dict[str, Any]], Dict[str, Any]]:
    """Return deduped answer spans plus cell fields for failure diagnosis.

    The strict headline remains ``answer_blocks@k``. These side-channel fields
    explain whether a miss was true content failure or a schema/path/ranking
    issue, without loosening the scorer.
    """

    deduped = dedup_spans(
        [dict(span) for span in answer_spans or [] if isinstance(span, Mapping)]
    )
    all_metrics = compute_block_metrics(deduped, gt_blocks)
    alias_metrics = compute_path_alias_metrics(deduped, gt_blocks)
    diagnosis_k = _diagnosis_k(metrics_k)
    rec_at_k = _recall_at_k(metrics, diagnosis_k)
    if rec_at_k is None:
        rec_at_k = compute_block_metrics(deduped[:diagnosis_k], gt_blocks).get("recall")
    diagnosis = diagnose_answer_spans(
        answer=answer or "",
        deduped_spans=deduped,
        rec_at_k=float(rec_at_k or 0.0),
        rec_all=float(all_metrics.get("recall") or 0.0),
        alias_recall=float(alias_metrics.get("recall") or 0.0),
    )
    return deduped, {
        "answer_span_count": len(deduped),
        "answer_top_spans": {str(k): deduped[: int(k)] for k in sorted(metrics_k)},
        "answer_all_metrics": all_metrics,
        "answer_path_alias_metrics": alias_metrics,
        "answer_diagnosis": diagnosis,
    }


def answer_diagnosis_from_cell(
    cell: Mapping[str, Any], *, diagnosis_k: int = 5
) -> Optional[str]:
    """Best-effort diagnosis for old and new sweep cell schemas."""

    value = cell.get("answer_diagnosis")
    if value:
        return str(value)
    metrics = cell.get("metrics") if isinstance(cell.get("metrics"), Mapping) else {}
    rec_at_k = _recall_at_k(metrics, diagnosis_k)
    spans = cell.get("answer_spans")
    gt_blocks = cell.get("gt_code_blocks") or cell.get("code_blocks") or []
    if isinstance(spans, Sequence) and isinstance(gt_blocks, Sequence):
        deduped, fields = build_answer_diagnostic_fields(
            answer=str(cell.get("answer") or ""),
            answer_spans=[span for span in spans if isinstance(span, Mapping)],
            gt_blocks=[block for block in gt_blocks if isinstance(block, Mapping)],
            metrics=metrics,
            metrics_k=[diagnosis_k],
        )
        if deduped or gt_blocks:
            return str(fields["answer_diagnosis"])
    if cell.get("format_failed"):
        return "format_gap"
    if rec_at_k is not None and rec_at_k >= 1.0:
        return "ok"
    all_metrics = cell.get("answer_all_metrics")
    alias_metrics = cell.get("answer_path_alias_metrics")
    rec_all = all_metrics.get("recall") if isinstance(all_metrics, Mapping) else None
    alias_recall = (
        alias_metrics.get("recall") if isinstance(alias_metrics, Mapping) else None
    )
    try:
        if rec_at_k is not None and rec_all is not None and float(rec_all) > rec_at_k:
            return "rank_gap"
        if (
            alias_recall is not None
            and rec_all is not None
            and float(alias_recall) > float(rec_all)
        ):
            return "path_alias_gap"
    except (TypeError, ValueError):
        return None
    return None


def answer_diagnosis_rates(
    cells: Sequence[Mapping[str, Any]], *, diagnosis_k: int = 5
) -> Dict[str, Optional[float]]:
    diagnoses = [
        answer_diagnosis_from_cell(cell, diagnosis_k=diagnosis_k) for cell in cells
    ]
    known = [diagnosis for diagnosis in diagnoses if diagnosis]
    if not known:
        return {label: None for label in ANSWER_DIAGNOSES}
    return {
        label: sum(1.0 for diagnosis in known if diagnosis == label) / len(known)
        for label in ANSWER_DIAGNOSES
    }


def format_answer_diagnosis_mix(
    rates: Mapping[str, Optional[float]], *, min_rate: float = 0.0
) -> str:
    parts = []
    for label in ANSWER_DIAGNOSES:
        rate = rates.get(label)
        if rate is None or rate <= min_rate:
            continue
        parts.append(f"{ANSWER_DIAGNOSIS_SHORT[label]}:{100 * rate:.0f}%")
    return ",".join(parts) if parts else "n/a"
