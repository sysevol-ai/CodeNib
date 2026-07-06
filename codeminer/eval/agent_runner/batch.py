# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Generic batch runner for agent-runner baseline tasks."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence, Set

from codeminer.eval.retrieval_eval import aggregate_metrics, average_metrics

from .baseline import BaselineTask, build_baseline_result_entry

BaselineTaskRunner = Callable[[BaselineTask], Any | Awaitable[Any]]


@dataclass(frozen=True)
class BaselineBatchSummary:
    """Summary of a baseline batch run."""

    total: int
    attempted: int
    succeeded: int
    failed: int
    skipped: int
    scored: int
    metrics: Mapping[str, Mapping[int, Mapping[str, float]]]
    result_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "scored": self.scored,
            "metrics": {
                scope: {int(k): dict(values) for k, values in per_k.items()}
                for scope, per_k in self.metrics.items()
            },
            "result_path": self.result_path,
        }


def baseline_done_ids(result_path: str | Path) -> Set[str]:
    """Return instance_ids already present in a baseline JSONL file."""

    path = Path(result_path)
    if not path.exists():
        return set()
    done: Set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            instance_id = obj.get("instance_id")
            if instance_id:
                done.add(str(instance_id))
    return done


async def run_baseline_batch(
    tasks: Sequence[BaselineTask],
    *,
    runner: BaselineTaskRunner,
    agent_name: str,
    model: Optional[str],
    metrics_k: Sequence[int],
    result_path: str | Path | None = None,
    resume: bool = False,
) -> BaselineBatchSummary:
    """Run a sync or async baseline runner over tasks and write JSONL records."""

    done = baseline_done_ids(result_path) if result_path and resume else set()
    aggregate = _empty_aggregate(metrics_k)
    result_file = _open_result_file(result_path)

    attempted = succeeded = failed = skipped = scored = 0
    try:
        for task in tasks:
            if task.instance_id in done:
                skipped += 1
                continue

            attempted += 1
            try:
                raw_result = runner(task)
                if inspect.isawaitable(raw_result):
                    raw_result = await raw_result
                entry = build_baseline_result_entry(
                    task=task,
                    agent_name=agent_name,
                    model=model,
                    result=raw_result,
                    metrics_k=metrics_k,
                )
            except Exception as exc:  # noqa: BLE001 - record and continue
                entry = build_baseline_result_entry(
                    task=task,
                    agent_name=agent_name,
                    model=model,
                    result=None,
                    metrics_k=metrics_k,
                    error=str(exc),
                )

            if entry["success"]:
                succeeded += 1
            else:
                failed += 1
            if entry["success"] and entry["metrics"] is not None:
                aggregate_metrics(aggregate, entry["metrics"])
                scored += 1
            _write_entry(result_file, entry)
    finally:
        if result_file is not None:
            result_file.close()

    return BaselineBatchSummary(
        total=len(tasks),
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        scored=scored,
        metrics=average_metrics(aggregate, scored),
        result_path=(
            str(Path(result_path).expanduser().resolve())
            if result_path is not None
            else None
        ),
    )


def _empty_aggregate(ks: Sequence[int]) -> Dict[str, Dict[int, Dict[str, float]]]:
    return {
        scope: {
            int(k): {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "hits": 0.0}
            for k in ks
        }
        for scope in ("files", "symbols")
    }


def _open_result_file(path: str | Path | None):
    if path is None:
        return None
    result_path = Path(path).expanduser().resolve()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    return result_path.open("a", encoding="utf-8")


def _write_entry(result_file: Any, entry: Mapping[str, Any]) -> None:
    if result_file is None:
        return
    result_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    result_file.flush()


__all__ = [
    "BaselineBatchSummary",
    "BaselineTaskRunner",
    "baseline_done_ids",
    "run_baseline_batch",
]
