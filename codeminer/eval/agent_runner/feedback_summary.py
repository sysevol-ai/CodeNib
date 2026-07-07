# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Small-run feedback summaries for agent-runner iterations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from .metrics import load_cell_jsons, metric_at_k, safe_mean

Cell = Dict[str, Any]


@dataclass(frozen=True)
class FeedbackArmSummary:
    """Raw behavior summary for one harness arm."""

    arm: str
    n: int
    success_rate: float
    metrics_meaningful_rate: float
    format_fail_rate: Optional[float]
    files_recall_at_k: Optional[float]
    answer_blocks_recall_at_k: Optional[float]
    total_tokens_mean: Optional[float]
    prompt_tokens_mean: Optional[float]
    cost_usd_mean: Optional[float]
    total_turns_mean: Optional[float]
    context_tokens_mean: Optional[float]
    tool_calls_mean: Optional[float]
    reads_mean: Optional[float]
    context_sources: Dict[str, int]
    trace_failure_categories: Dict[str, int]
    failure_groups: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arm": self.arm,
            "n": self.n,
            "success_rate": self.success_rate,
            "metrics_meaningful_rate": self.metrics_meaningful_rate,
            "format_fail_rate": self.format_fail_rate,
            "files_recall_at_k": self.files_recall_at_k,
            "answer_blocks_recall_at_k": self.answer_blocks_recall_at_k,
            "total_tokens_mean": self.total_tokens_mean,
            "prompt_tokens_mean": self.prompt_tokens_mean,
            "cost_usd_mean": self.cost_usd_mean,
            "total_turns_mean": self.total_turns_mean,
            "context_tokens_mean": self.context_tokens_mean,
            "tool_calls_mean": self.tool_calls_mean,
            "reads_mean": self.reads_mean,
            "context_sources": dict(self.context_sources),
            "trace_failure_categories": dict(self.trace_failure_categories),
            "failure_groups": dict(self.failure_groups),
        }


@dataclass(frozen=True)
class FeedbackArmDelta:
    """Difference between one arm summary and a baseline summary."""

    arm: str
    baseline: str
    success_rate_delta: Optional[float]
    files_recall_delta: Optional[float]
    answer_blocks_recall_delta: Optional[float]
    total_tokens_delta: Optional[float]
    cost_usd_delta: Optional[float]
    total_turns_delta: Optional[float]
    context_tokens_delta: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arm": self.arm,
            "baseline": self.baseline,
            "success_rate_delta": self.success_rate_delta,
            "files_recall_delta": self.files_recall_delta,
            "answer_blocks_recall_delta": self.answer_blocks_recall_delta,
            "total_tokens_delta": self.total_tokens_delta,
            "cost_usd_delta": self.cost_usd_delta,
            "total_turns_delta": self.total_turns_delta,
            "context_tokens_delta": self.context_tokens_delta,
        }


@dataclass(frozen=True)
class FeedbackSummary:
    """Feedback report for a small sweep or query-sweep result set."""

    k: int
    cell_count: int
    baseline: Optional[str]
    arms: List[FeedbackArmSummary]
    deltas: List[FeedbackArmDelta]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "k": self.k,
            "cell_count": self.cell_count,
            "baseline": self.baseline,
            "arms": [arm.to_dict() for arm in self.arms],
            "deltas": [delta.to_dict() for delta in self.deltas],
        }


def summarize_feedback_cells(
    cells: Sequence[Cell],
    *,
    baseline: Optional[str] = None,
    k: int = 5,
    arm_field: str = "subset_id",
) -> FeedbackSummary:
    """Summarize small-run cells without scorer-specific answer repair."""

    by_arm: Dict[str, List[Cell]] = {}
    for cell in cells:
        arm = str(cell.get(arm_field) or cell.get("arm") or "(unknown)")
        by_arm.setdefault(arm, []).append(cell)

    arms = [summarize_feedback_arm(arm, by_arm[arm], k=k) for arm in sorted(by_arm)]
    baseline_summary = next((arm for arm in arms if arm.arm == baseline), None)
    deltas = (
        [
            _delta_from_baseline(arm, baseline_summary)
            for arm in arms
            if arm.arm != baseline_summary.arm
        ]
        if baseline_summary is not None
        else []
    )
    return FeedbackSummary(
        k=int(k),
        cell_count=len(cells),
        baseline=baseline if baseline_summary is not None else None,
        arms=arms,
        deltas=deltas,
    )


def summarize_feedback_arm(
    arm: str,
    cells: Sequence[Cell],
    *,
    k: int = 5,
) -> FeedbackArmSummary:
    """Summarize raw behavior for one arm."""

    arm_cells = list(cells)
    n = len(arm_cells)
    successful = [cell for cell in arm_cells if cell.get("success")]
    meaningful = [cell for cell in successful if cell.get("metrics_meaningful")]

    context_sources: Counter[str] = Counter()
    trace_failures: Counter[str] = Counter()
    failure_groups: Counter[str] = Counter()
    for cell in arm_cells:
        trace_summary = _trace_summary(cell)
        context_sources.update(_counter_from_mapping(trace_summary, "context_sources"))
        trace_failure_categories = _string_list(trace_summary.get("failure_categories"))
        trace_failures.update(trace_failure_categories)
        failure_groups.update(_failure_groups(cell, trace_failure_categories))

    return FeedbackArmSummary(
        arm=arm,
        n=n,
        success_rate=(len(successful) / n if n else 0.0),
        metrics_meaningful_rate=(len(meaningful) / n if n else 0.0),
        format_fail_rate=(
            sum(1 for cell in meaningful if cell.get("format_failed")) / len(meaningful)
            if meaningful
            else None
        ),
        files_recall_at_k=safe_mean(
            [metric_at_k(cell, "files", k, field="recall") for cell in meaningful]
        ),
        answer_blocks_recall_at_k=safe_mean(
            [
                metric_at_k(cell, "answer_blocks", k, field="recall")
                for cell in meaningful
            ]
        ),
        total_tokens_mean=_mean_field(successful, "total_tokens"),
        prompt_tokens_mean=_mean_field(successful, "prompt_tokens"),
        cost_usd_mean=_mean_field(successful, "cost_usd"),
        total_turns_mean=_mean_field(successful, "total_turns"),
        context_tokens_mean=safe_mean(
            [
                _number(_trace_summary(cell).get("context_tokens_estimate"))
                for cell in successful
            ]
        ),
        tool_calls_mean=safe_mean(
            [
                _number(_trace_summary(cell).get("tool_call_count"))
                for cell in successful
            ]
        ),
        reads_mean=safe_mean(
            [_number(_trace_summary(cell).get("read_count")) for cell in successful]
        ),
        context_sources=dict(sorted(context_sources.items())),
        trace_failure_categories=dict(sorted(trace_failures.items())),
        failure_groups=dict(sorted(failure_groups.items())),
    )


def load_feedback_summary(
    result_dir: Union[Path, str],
    *,
    baseline: Optional[str] = None,
    k: int = 5,
    arm_field: str = "subset_id",
) -> FeedbackSummary:
    """Load ``cells/*.json`` from a result directory and summarize them."""

    cells = load_cell_jsons(Path(result_dir).joinpath("cells"))
    return summarize_feedback_cells(
        cells,
        baseline=baseline,
        k=k,
        arm_field=arm_field,
    )


def _delta_from_baseline(
    arm: FeedbackArmSummary,
    baseline: FeedbackArmSummary,
) -> FeedbackArmDelta:
    return FeedbackArmDelta(
        arm=arm.arm,
        baseline=baseline.arm,
        success_rate_delta=_delta(arm.success_rate, baseline.success_rate),
        files_recall_delta=_delta(
            arm.files_recall_at_k,
            baseline.files_recall_at_k,
        ),
        answer_blocks_recall_delta=_delta(
            arm.answer_blocks_recall_at_k,
            baseline.answer_blocks_recall_at_k,
        ),
        total_tokens_delta=_delta(arm.total_tokens_mean, baseline.total_tokens_mean),
        cost_usd_delta=_delta(arm.cost_usd_mean, baseline.cost_usd_mean),
        total_turns_delta=_delta(arm.total_turns_mean, baseline.total_turns_mean),
        context_tokens_delta=_delta(
            arm.context_tokens_mean,
            baseline.context_tokens_mean,
        ),
    )


def _trace_summary(cell: Mapping[str, Any]) -> Mapping[str, Any]:
    trace_summary = cell.get("trace_summary")
    return trace_summary if isinstance(trace_summary, Mapping) else {}


def _counter_from_mapping(data: Mapping[str, Any], key: str) -> Counter[str]:
    raw = data.get(key)
    counter: Counter[str] = Counter()
    if not isinstance(raw, Mapping):
        return counter
    for item, count in raw.items():
        if isinstance(count, int):
            counter[str(item)] += count
    return counter


def _failure_groups(
    cell: Mapping[str, Any],
    trace_failure_categories: Sequence[str],
) -> List[str]:
    groups: set[str] = set()
    if not cell.get("success"):
        groups.add("infrastructure")
    if (
        cell.get("format_failed")
        or "answer_contract_missing" in trace_failure_categories
    ):
        groups.add("format")
    if any(
        category in trace_failure_categories
        for category in ("tool_error", "turn_budget_exhausted", "no_successful_read")
    ):
        groups.add("context_runtime")
    return sorted(groups)


def _mean_field(cells: Sequence[Mapping[str, Any]], field: str) -> Optional[float]:
    return safe_mean([_number(cell.get(field)) for cell in cells])


def _delta(value: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if value is None or baseline is None:
        return None
    return value - baseline


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, str)):
        return []
    out: List[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


__all__ = [
    "FeedbackArmDelta",
    "FeedbackArmSummary",
    "FeedbackSummary",
    "load_feedback_summary",
    "summarize_feedback_arm",
    "summarize_feedback_cells",
]
