# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Lightweight task and result contracts for localization baselines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from codenib.eval.retrieval_eval import compute_metrics, normalize_file_path


@dataclass(frozen=True)
class BaselineTask:
    """One localization task handed to an internal or external agent."""

    instance_id: str
    query: str
    repo_path: str
    context: Mapping[str, Any] = field(default_factory=dict)
    target_files: Tuple[str, ...] = ()
    target_symbols: Tuple[str, ...] = ()
    repo_name: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dataset_instance(
        cls,
        instance: Mapping[str, Any],
        *,
        repo_path: str,
        target_files: Sequence[str] = (),
        target_symbols: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> "BaselineTask":
        instance_id = str(instance["instance_id"])
        repo_name = str(instance.get("repo") or "")
        query = str(instance.get("problem_statement") or "")
        context: Dict[str, Any] = {
            "instance_id": instance_id,
            "issue_title": f"[{instance_id}] {repo_name}" if repo_name else instance_id,
            "issue_body": query,
        }
        if repo_name:
            context["repo_name"] = repo_name
        if instance.get("hints_text"):
            context["hints"] = instance["hints_text"]
        return cls(
            instance_id=instance_id,
            query=query,
            repo_path=str(repo_path),
            context=context,
            target_files=tuple(target_files),
            target_symbols=tuple(target_symbols),
            repo_name=repo_name or None,
            metadata=dict(metadata or {}),
        )

    def to_agent_call(self) -> Dict[str, Any]:
        """Return the standard ``locate_code`` call payload for this task."""

        return {
            "query_text": self.query,
            "repo_path": self.repo_path,
            "context": dict(self.context),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "query": self.query,
            "repo_path": self.repo_path,
            "context": dict(self.context),
            "target_files": list(self.target_files),
            "target_symbols": list(self.target_symbols),
            "repo_name": self.repo_name,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class BaselineLocation:
    """One location emitted by a baseline agent."""

    name: str
    type: str
    file_path: str
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    action: Optional[str] = None
    description: str = ""

    @classmethod
    def from_raw(cls, value: Any) -> "BaselineLocation":
        return cls(
            name=str(_field(value, "name") or ""),
            type=str(_field(value, "type") or ""),
            file_path=str(_field(value, "file_path") or ""),
            line_start=_optional_int(_field(value, "line_start")),
            line_end=_optional_int(_field(value, "line_end")),
            action=_optional_str(_field(value, "action")),
            description=str(_field(value, "description") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "action": self.action,
            "description": self.description,
        }

    def symbol_id(self) -> Optional[str]:
        if not self.name or not self.file_path:
            return None
        file_norm = normalize_file_path(self.file_path)
        if not file_norm:
            return None
        return f"{file_norm}:{self.name}"


@dataclass(frozen=True)
class BaselineRunResult:
    """Normalized result returned by an internal, external, or LSP baseline."""

    success: bool
    locations: Tuple[BaselineLocation, ...] = ()
    usage: Optional[Mapping[str, Any]] = None
    error_message: Optional[str] = None
    repo_path: Optional[str] = None
    raw_output: Any = None

    @classmethod
    def from_raw(
        cls,
        result: Any,
        *,
        error: Optional[str] = None,
    ) -> "BaselineRunResult":
        if error or result is None or not bool(getattr(result, "success", False)):
            return cls(
                success=False,
                error_message=error
                or (getattr(result, "error_message", None) if result else None)
                or "no result",
                repo_path=getattr(result, "repo_path", None) if result else None,
                raw_output=result,
            )

        return cls(
            success=True,
            locations=tuple(
                BaselineLocation.from_raw(item)
                for item in (getattr(result, "locations", None) or [])
            ),
            usage=getattr(result, "usage", None),
            repo_path=getattr(result, "repo_path", None),
            raw_output=result,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "locations": [location.to_dict() for location in self.locations],
            "usage": dict(self.usage) if isinstance(self.usage, Mapping) else None,
            "error_message": self.error_message,
            "repo_path": self.repo_path,
        }


def build_baseline_result_entry(
    *,
    task: BaselineTask,
    agent_name: str,
    model: Optional[str],
    result: Any,
    metrics_k: Sequence[int],
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one JSON-ready baseline record with predictions and metrics."""

    normalized = BaselineRunResult.from_raw(result, error=error)
    base = {
        "instance_id": task.instance_id,
        "agent": agent_name,
        "model": model,
        "target_files": list(task.target_files),
        "target_symbols": list(task.target_symbols),
    }
    if not normalized.success:
        base.update(
            {
                "success": False,
                "error": normalized.error_message or "no result",
                "metric_k_files": [],
                "metric_k_node_ids": [],
                "locations": [],
                "usage": None,
                "metrics": None,
            }
        )
        return base

    predicted_files = unique_location_files(normalized.locations)
    predicted_symbols = location_symbol_ids(normalized.locations)
    metrics = score_baseline_predictions(
        predicted_files,
        predicted_symbols,
        task.target_files,
        task.target_symbols,
        metrics_k,
    )
    base.update(
        {
            "success": True,
            "metric_k_files": predicted_files,
            "metric_k_node_ids": predicted_symbols,
            "locations": [location.to_dict() for location in normalized.locations],
            "usage": normalized.usage,
            "metrics": metrics,
            "error": None,
        }
    )
    return base


def unique_location_files(locations: Sequence[BaselineLocation]) -> List[str]:
    """Return unique location file paths in prediction order."""

    seen = set()
    out: List[str] = []
    for location in locations:
        if location.file_path and location.file_path not in seen:
            out.append(location.file_path)
            seen.add(location.file_path)
    return out


def location_symbol_ids(locations: Sequence[BaselineLocation]) -> List[str]:
    """Return ``file:symbol`` predictions in baseline output order."""

    out: List[str] = []
    for location in locations:
        symbol_id = location.symbol_id()
        if symbol_id is not None:
            out.append(symbol_id)
    return out


def score_baseline_predictions(
    predicted_files: Sequence[str],
    predicted_symbols: Sequence[str],
    target_files: Sequence[str],
    target_symbols: Sequence[str],
    ks: Sequence[int],
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """Score baseline file and symbol predictions at each requested cutoff."""

    metrics: Dict[str, Dict[int, Dict[str, float]]] = {"files": {}, "symbols": {}}
    for k in ks:
        metrics["files"][k] = compute_metrics(list(predicted_files[:k]), target_files)
        metrics["symbols"][k] = compute_metrics(
            list(predicted_symbols[:k]), target_symbols
        )
    return metrics


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


__all__ = [
    "BaselineLocation",
    "BaselineRunResult",
    "BaselineTask",
    "build_baseline_result_entry",
    "location_symbol_ids",
    "score_baseline_predictions",
    "unique_location_files",
]
