# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Trace replay summaries for agent-runner evaluations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


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
    lsp_route_context: Dict[str, Any]
    lsp_route_tool_calls: List[Dict[str, Any]]
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
            "lsp_route_context": dict(self.lsp_route_context),
            "lsp_route_tool_calls": list(self.lsp_route_tool_calls),
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
    lsp_route_context: Dict[str, Any] = {}
    lsp_route_tool_calls: List[Dict[str, Any]] = []
    next_llm_call_ms_by_turn = _next_llm_call_ms_by_turn(events)
    next_llm_call_turn_by_turn = _next_llm_call_turn_by_turn(events)

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
            if tool == "lsp_route":
                lsp_route_tool_calls.append(
                    _lsp_route_tool_call_summary(
                        data,
                        turn=turn,
                        timestamp_ms=_event_timestamp_ms(event),
                        model_can_use_turn=(
                            next_llm_call_turn_by_turn.get(turn)
                            if turn is not None
                            else None
                        ),
                        model_can_use_ms=(
                            next_llm_call_ms_by_turn.get(turn)
                            if turn is not None
                            else None
                        ),
                    )
                )
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
        elif kind == "lsp_route_context":
            lsp_route_context = _lsp_route_context_summary(
                data,
                timestamp_ms=_event_timestamp_ms(event),
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
        lsp_route_context=lsp_route_context,
        lsp_route_tool_calls=lsp_route_tool_calls,
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


def _event_timestamp_ms(event: Any) -> Optional[float]:
    if isinstance(event, Mapping):
        raw = event.get("timestamp_ms")
    else:
        raw = getattr(event, "timestamp_ms", None)
    return float(raw) if isinstance(raw, (int, float)) else None


def _event_data(event: Any) -> Mapping[str, Any]:
    if isinstance(event, Mapping):
        data = event.get("data")
    else:
        data = getattr(event, "data", None)
    return data if isinstance(data, Mapping) else {}


def _next_llm_call_ms_by_turn(events: Sequence[Any]) -> Dict[int, float]:
    out: Dict[int, float] = {}
    llm_calls = [
        (_event_turn(event), _event_timestamp_ms(event))
        for event in events
        if _event_kind(event) == "llm_call"
    ]
    for event in events:
        if _event_kind(event) != "tool_call":
            continue
        turn = _event_turn(event)
        if turn is None:
            continue
        candidates = [
            timestamp
            for candidate_turn, timestamp in llm_calls
            if (
                candidate_turn is not None
                and candidate_turn > turn
                and timestamp is not None
            )
        ]
        if candidates:
            out[turn] = min(candidates)
    return out


def _next_llm_call_turn_by_turn(events: Sequence[Any]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    llm_turns = [
        turn
        for turn in (
            _event_turn(event) for event in events if _event_kind(event) == "llm_call"
        )
        if turn is not None
    ]
    for event in events:
        if _event_kind(event) != "tool_call":
            continue
        turn = _event_turn(event)
        if turn is None:
            continue
        candidates = [
            candidate_turn for candidate_turn in llm_turns if candidate_turn > turn
        ]
        if candidates:
            out[turn] = min(candidates)
    return out


def _lsp_route_context_summary(
    data: Mapping[str, Any],
    *,
    timestamp_ms: Optional[float],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in (
        "status",
        "reason",
        "seeds",
        "seed_source",
        "seed_policy",
        "route_count",
        "context_chars",
        "duration_ms",
        "lsp_provider",
        "lsp_result_fingerprint",
        "lsp_result_preview",
        "route_args",
        "route_fingerprint",
        "route_preview",
    ):
        if key in data:
            out[key] = data[key]
    if timestamp_ms is not None:
        out["event_ms"] = timestamp_ms
    if data.get("status") == "offered":
        out["visible_turn"] = 0
        duration_ms = data.get("duration_ms")
        if isinstance(duration_ms, (int, float)):
            out["route_visible_ms"] = float(duration_ms)
    return out


def _lsp_route_tool_call_summary(
    data: Mapping[str, Any],
    *,
    turn: Optional[int],
    timestamp_ms: Optional[float],
    model_can_use_turn: Optional[int],
    model_can_use_ms: Optional[float],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if turn is not None:
        out["turn"] = turn
    for key in (
        "status",
        "result_count",
        "duration_ms",
        "lsp_provider",
        "lsp_result_fingerprint",
        "lsp_result_preview",
        "route_args",
        "route_fingerprint",
        "route_preview",
    ):
        if key in data:
            out[key] = data[key]
    if timestamp_ms is not None:
        out["tool_result_ms"] = timestamp_ms
    if model_can_use_turn is not None:
        out["model_can_use_turn"] = model_can_use_turn
        out["extra_model_round_trips"] = max(0, model_can_use_turn - (turn or 0))
    if model_can_use_ms is not None:
        out["model_can_use_ms"] = model_can_use_ms
    args = data.get("arguments")
    if isinstance(args, Mapping):
        compact_args: Dict[str, Any] = {}
        for key in ("symbols", "query", "top_k", "include_neighbors"):
            if key in args:
                compact_args[key] = args[key]
        out["arguments"] = compact_args
    return out


def _sorted_counts(counter: Counter[str]) -> Dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


__all__ = ["AgentTraceSummary", "summarize_agent_trace"]
