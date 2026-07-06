# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared agent-localization scoring for sweep CLIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from codeminer.agent import has_localization_contract
from codeminer.eval.retrieval_eval import (
    dedup_spans,
    nodes_to_spans,
    parse_answer_spans,
    resolve_symbol_spans,
    score_agent_localization,
    score_localization_spans,
    spans_overlap,
)


@dataclass(frozen=True)
class AgentLocalizationEvaluation:
    """Scoring outputs written into an agent sweep cell record."""

    metrics: dict[str, Any]
    metrics_meaningful: bool
    target_files: list[str]
    target_symbols: list[str]
    gt_code_blocks: list[dict[str, Any]]
    answer_spans: list[dict[str, Any]]
    retrieval_spans: list[dict[str, Any]]
    preload_contribution: Optional[float]
    format_failed: bool

    def to_record_fields(self) -> dict[str, Any]:
        """Return JSON-ready fields shared by sweep cell records."""
        return {
            "format_failed": self.format_failed,
            "metrics": self.metrics,
            "metrics_meaningful": self.metrics_meaningful,
            "target_files": self.target_files,
            "target_symbols": self.target_symbols,
            "gt_code_blocks": self.gt_code_blocks,
            "answer_spans": self.answer_spans,
            "retrieval_spans": self.retrieval_spans,
            "preload_contribution": self.preload_contribution,
        }


def evaluate_agent_localization(
    *,
    answer: str,
    file_read_paths: Sequence[str],
    nodes: Sequence[Any],
    target_files: Sequence[str],
    target_symbols: Sequence[str],
    gt_blocks: Sequence[Mapping[str, Any]],
    metrics_k: Sequence[int],
    repo_path: str,
    symbol_span_index: Optional[Mapping[tuple[str, str], tuple[int, int]]] = None,
    preload_candidates: Optional[Sequence[Mapping[str, Any]]] = None,
    metrics_meaningful: Optional[bool] = None,
) -> AgentLocalizationEvaluation:
    """Evaluate one agent answer without applying runtime policy.

    The returned fields keep raw file/symbol metrics, span-overlap metrics,
    format failure, and pre-load contribution separate so experiment reports can
    show whether a change improved localization or just repaired formatting.
    """
    answer_text = answer or ""
    target_files_list = list(target_files)
    target_symbols_list = list(target_symbols)
    gt_blocks_list = [dict(block) for block in gt_blocks]

    metrics = score_agent_localization(
        answer=answer_text,
        file_read_paths=file_read_paths,
        nodes=nodes,
        target_files=target_files_list,
        target_symbols=target_symbols_list,
        ks=metrics_k,
        repo_path=repo_path,
    )
    answer_spans = parse_answer_spans(answer_text, repo_path)
    if symbol_span_index:
        answer_spans += resolve_symbol_spans(answer_text, symbol_span_index, repo_path)
    retrieval_spans = nodes_to_spans(nodes)
    metrics.update(
        score_localization_spans(
            answer_spans=answer_spans,
            retrieval_spans=retrieval_spans,
            gt_blocks=gt_blocks_list,
            ks=metrics_k,
        )
    )

    return AgentLocalizationEvaluation(
        metrics=metrics,
        metrics_meaningful=(
            bool(target_files_list or target_symbols_list or gt_blocks_list)
            if metrics_meaningful is None
            else metrics_meaningful
        ),
        target_files=target_files_list,
        target_symbols=target_symbols_list,
        gt_code_blocks=gt_blocks_list,
        answer_spans=answer_spans,
        retrieval_spans=retrieval_spans,
        preload_contribution=_preload_contribution(
            answer_spans, preload_candidates or ()
        ),
        format_failed=(not has_localization_contract(answer_text) and not answer_spans),
    )


def _preload_contribution(
    answer_spans: Sequence[Mapping[str, Any]],
    preload_candidates: Sequence[Mapping[str, Any]],
) -> Optional[float]:
    if not answer_spans or not preload_candidates:
        return None
    answer_dedup = dedup_spans([dict(span) for span in answer_spans])
    if not answer_dedup:
        return None
    return sum(
        1
        for answer_span in answer_dedup
        if any(
            spans_overlap(answer_span, candidate) for candidate in preload_candidates
        )
    ) / len(answer_dedup)
