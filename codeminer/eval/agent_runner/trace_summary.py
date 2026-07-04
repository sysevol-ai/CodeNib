# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Trace replay summaries for agent-runner evaluations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional


@dataclass(frozen=True)
class AgentTraceSummary:
    """Evaluation-friendly summary of one agent runtime trace."""

    schema_version: Optional[int]
    event_count: int
    max_turn: int
    tool_call_count: int
    tool_error_count: int
    tools: Dict[str, int]
    tool_errors: Dict[str, int]
    read_count: int
    read_paths: List[str]
    context_entry_count: int
    context_tokens_estimate: int
    context_by_state: Dict[str, int]
    context_sources: Dict[str, int]
    compaction_count: int
    max_turns_exhausted: bool
    forced_final_answer: bool
    answer_contract_present: Optional[bool]
    final_answer_source: Optional[str]
    failure_categories: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_count": self.event_count,
            "max_turn": self.max_turn,
            "tool_call_count": self.tool_call_count,
            "tool_error_count": self.tool_error_count,
            "tools": dict(self.tools),
            "tool_errors": dict(self.tool_errors),
            "read_count": self.read_count,
            "read_paths": list(self.read_paths),
            "context_entry_count": self.context_entry_count,
            "context_tokens_estimate": self.context_tokens_estimate,
            "context_by_state": dict(self.context_by_state),
            "context_sources": dict(self.context_sources),
            "compaction_count": self.compaction_count,
            "max_turns_exhausted": self.max_turns_exhausted,
            "forced_final_answer": self.forced_final_answer,
            "answer_contract_present": self.answer_contract_present,
            "final_answer_source": self.final_answer_source,
            "failure_categories": list(self.failure_categories),
        }


def summarize_agent_trace(trace_or_result: Any) -> AgentTraceSummary:
    """Summarize runtime trace events without applying scorer normalization."""

    trace = _extract_trace(trace_or_result)
    schema_version = _schema_version(trace)
    events = list(_iter_events(trace))
    contexts = list(_iter_context_entries(trace))

    tools: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    read_paths: List[str] = []
    max_turn = 0
    tool_call_count = 0
    compaction_count = 0
    max_turns_exhausted = False
    forced_final_answer = False
    answer_contract_present: Optional[bool] = None
    final_answer_source: Optional[str] = None

    for event in events:
        kind = _event_kind(event)
        turn = _event_turn(event)
        if turn is not None:
            max_turn = max(max_turn, turn)
        data = _event_data(event)

        if kind == "tool_call":
            tool_call_count += 1
            tool = str(data.get("tool") or "(unknown)")
            tools[tool] += 1
            if data.get("status") == "error":
                tool_errors[tool] += 1
        elif kind == "read" and data.get("status") == "ok":
            path = data.get("path")
            if path:
                read_paths.append(str(path))
        elif kind == "context_compacted":
            compaction_count += 1
        elif kind == "max_turns_exhausted":
            max_turns_exhausted = True
        elif kind == "final_answer_forced":
            forced_final_answer = True
        elif kind == "final_answer":
            if "has_contract" in data:
                answer_contract_present = bool(data.get("has_contract"))
            final_answer_source = (
                str(data["source"]) if data.get("source") is not None else None
            )

    context_by_state: Counter[str] = Counter()
    context_sources: Counter[str] = Counter()
    context_tokens_estimate = 0
    for entry in contexts:
        state = entry.get("state")
        if state:
            context_by_state[str(state)] += 1
        source = entry.get("source")
        if source:
            context_sources[str(source)] += 1
        token_estimate = entry.get("token_estimate")
        if isinstance(token_estimate, int):
            context_tokens_estimate += token_estimate

    failure_categories: List[str] = []
    if tool_errors:
        failure_categories.append("tool_error")
    if max_turns_exhausted:
        failure_categories.append("turn_budget_exhausted")
    if answer_contract_present is False:
        failure_categories.append("answer_contract_missing")
    if tool_call_count and not read_paths:
        failure_categories.append("no_successful_read")

    return AgentTraceSummary(
        schema_version=schema_version,
        event_count=len(events),
        max_turn=max_turn,
        tool_call_count=tool_call_count,
        tool_error_count=sum(tool_errors.values()),
        tools=_sorted_counts(tools),
        tool_errors=_sorted_counts(tool_errors),
        read_count=len(read_paths),
        read_paths=list(dict.fromkeys(read_paths)),
        context_entry_count=len(contexts),
        context_tokens_estimate=context_tokens_estimate,
        context_by_state=_sorted_counts(context_by_state),
        context_sources=_sorted_counts(context_sources),
        compaction_count=compaction_count,
        max_turns_exhausted=max_turns_exhausted,
        forced_final_answer=forced_final_answer,
        answer_contract_present=answer_contract_present,
        final_answer_source=final_answer_source,
        failure_categories=failure_categories,
    )


def _extract_trace(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping) and ("events" in value or "context" in value):
        return value
    return getattr(value, "trace", value)


def _schema_version(trace: Any) -> Optional[int]:
    if trace is None:
        return None
    if isinstance(trace, Mapping):
        raw = trace.get("schema_version")
        return raw if isinstance(raw, int) else None
    if hasattr(trace, "to_dict"):
        raw = trace.to_dict().get("schema_version")
        return raw if isinstance(raw, int) else None
    return None


def _iter_events(trace: Any) -> Iterable[Any]:
    if trace is None:
        return []
    if isinstance(trace, Mapping):
        events = trace.get("events")
        return events if isinstance(events, list) else []
    return list(getattr(trace, "events", []) or [])


def _iter_context_entries(trace: Any) -> Iterable[Mapping[str, Any]]:
    if trace is None:
        return []
    if isinstance(trace, Mapping):
        entries = trace.get("context")
    else:
        entries = getattr(trace, "context", None)
    if entries is None:
        return []
    return [_entry_to_mapping(entry) for entry in entries]


def _entry_to_mapping(entry: Any) -> Mapping[str, Any]:
    if isinstance(entry, Mapping):
        return entry
    if hasattr(entry, "to_dict"):
        return entry.to_dict()
    return {
        "source": getattr(entry, "source", None),
        "state": getattr(entry, "state", None),
        "token_estimate": getattr(entry, "token_estimate", None),
    }


def _event_kind(event: Any) -> Optional[str]:
    if isinstance(event, Mapping):
        raw = event.get("kind")
    else:
        raw = getattr(event, "kind", None)
    return str(raw) if raw is not None else None


def _event_turn(event: Any) -> Optional[int]:
    if isinstance(event, Mapping):
        raw = event.get("turn")
    else:
        raw = getattr(event, "turn", None)
    return raw if isinstance(raw, int) else None


def _event_data(event: Any) -> Mapping[str, Any]:
    if isinstance(event, Mapping):
        data = event.get("data")
    else:
        data = getattr(event, "data", None)
    return data if isinstance(data, Mapping) else {}


def _sorted_counts(counter: Counter[str]) -> Dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


__all__ = ["AgentTraceSummary", "summarize_agent_trace"]
