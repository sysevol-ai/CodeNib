# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compact summaries for persisted agent-run trace artifacts.

Sweep cells already persist the full ``trace`` payload. This module provides the
stable, replay-adjacent view needed for feedback iteration: load a completed
cell from disk, inspect turns/tools/context/scheduler events, and decide what to
fix without scraping raw stdout.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

TRACE_SUMMARY_VERSION = 1
TRACE_REPLAY_VERSION = 2


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _trace_from_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    trace = payload.get("trace")
    if isinstance(trace, Mapping):
        return trace
    return payload


def _events(trace: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return [event for event in trace.get("events") or [] if isinstance(event, Mapping)]


def _context(trace: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return [entry for entry in trace.get("context") or [] if isinstance(entry, Mapping)]


def _event_kind(event: Mapping[str, Any]) -> str:
    return str(event.get("kind") or "")


def _event_data(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(event.get("data"))


def _event_turn(event: Mapping[str, Any]) -> Optional[int]:
    try:
        return int(event.get("turn"))
    except (TypeError, ValueError):
        return None


def _first_turn(events: Iterable[Mapping[str, Any]]) -> Optional[int]:
    turns = [
        turn for turn in (_event_turn(event) for event in events) if turn is not None
    ]
    return min(turns) if turns else None


def _sorted_counter(counter: Counter) -> Dict[str, int]:
    return {str(key): int(counter[key]) for key in sorted(counter)}


def _unique(values: Iterable[Any]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        text = str(value) if value is not None else ""
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _has_content_message(message: Mapping[str, Any]) -> bool:
    role = str(message.get("role") or "")
    if not role:
        return False
    if "content" in message:
        return True
    return role == "assistant" and bool(message.get("tool_calls"))


def _persisted_messages(cell: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    trace = _mapping(cell.get("trace"))
    raw = (
        cell.get("message_replay")
        or trace.get("message_replay")
        or trace.get("messages")
        or cell.get("messages")
        or []
    )
    return [message for message in raw if isinstance(message, Mapping)]


def _persisted_tool_schemas(cell: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    trace = _mapping(cell.get("trace"))
    raw = cell.get("tool_schema_replay") or trace.get("tool_schema_replay") or []
    return [schema for schema in raw if isinstance(schema, Mapping)]


def _event_has_full_content(event: Mapping[str, Any]) -> bool:
    data = _event_data(event)
    return any(
        key in data and data.get(key) is not None
        for key in ("content", "text", "result", "result_text", "tool_result")
    )


def summarize_replay_readiness(cell: Mapping[str, Any]) -> Dict[str, Any]:
    """Assess whether a cell carries enough evidence for exact replay.

    The existing trace is event-replayable, but exact provider-neutral replay
    requires the full message transcript or full assistant/tool-result content.
    This summary makes that gap explicit instead of letting consumers infer it
    from missing fields.
    """

    trace = _mapping(cell.get("trace"))
    events = _events(trace)
    messages = _persisted_messages(cell)
    tool_schemas = _persisted_tool_schemas(cell)
    assistant_events = [event for event in events if _event_kind(event) == "assistant"]
    tool_events = [event for event in events if _event_kind(event) == "tool_call"]
    llm_call_events = [event for event in events if _event_kind(event) == "llm_call"]
    llm_call_tool_events = [
        event
        for event in llm_call_events
        if int(_event_data(event).get("tool_count") or 0) > 0
    ]
    tool_events_with_envelope = [
        event for event in tool_events if _event_data(event).get("envelope")
    ]
    llm_call_events_with_message_indexes = [
        event
        for event in llm_call_events
        if isinstance(_event_data(event).get("message_replay_indexes"), list)
    ]
    llm_call_events_with_tool_schema_indexes = [
        event
        for event in llm_call_tool_events
        if len(_event_data(event).get("tool_schema_indexes") or [])
        == int(_event_data(event).get("tool_count") or 0)
    ]
    message_roles = Counter(
        str(message.get("role") or "unknown") for message in messages
    )
    content_messages = [
        message for message in messages if _has_content_message(message)
    ]
    has_full_messages = bool(messages) and len(content_messages) == len(messages)

    assistant_content_events = [
        event for event in assistant_events if _event_has_full_content(event)
    ]
    tool_result_events = [
        event for event in tool_events if _event_has_full_content(event)
    ]
    tool_message_count = int(message_roles.get("tool", 0))
    assistant_message_count = int(message_roles.get("assistant", 0))
    assistant_content_replayable = bool(
        (has_full_messages and assistant_message_count >= len(assistant_events))
        or len(assistant_content_events) >= len(assistant_events)
    )
    tool_result_replayable = bool(
        (has_full_messages and tool_message_count >= len(tool_events))
        or len(tool_result_events) >= len(tool_events)
    )
    llm_call_messages_replayable = bool(
        not llm_call_events
        or len(llm_call_events_with_message_indexes) == len(llm_call_events)
    )
    llm_call_tool_schemas_replayable = bool(
        not llm_call_tool_events
        or (
            bool(tool_schemas)
            and len(llm_call_events_with_tool_schema_indexes)
            == len(llm_call_tool_events)
        )
    )

    blocking_gaps: List[str] = []
    if not has_full_messages:
        blocking_gaps.append("missing_message_transcript")
    if assistant_events and not assistant_content_replayable:
        blocking_gaps.append("assistant_content_not_persisted")
    if tool_events and not tool_result_replayable:
        blocking_gaps.append("tool_result_content_not_persisted")
    if tool_events and len(tool_events_with_envelope) < len(tool_events):
        blocking_gaps.append("tool_envelope_incomplete")
    if llm_call_events and not llm_call_messages_replayable:
        blocking_gaps.append("llm_call_messages_not_indexed")
    if llm_call_tool_events and not llm_call_tool_schemas_replayable:
        blocking_gaps.append("llm_call_tool_schemas_not_indexed")

    return {
        "readiness_version": 1,
        "event_replayable": bool(events),
        "exact_message_replayable": bool(
            has_full_messages
            and assistant_content_replayable
            and tool_result_replayable
            and llm_call_messages_replayable
            and llm_call_tool_schemas_replayable
        ),
        "has_message_transcript": has_full_messages,
        "message_count": len(messages),
        "message_roles": _sorted_counter(message_roles),
        "tool_schema_count": len(tool_schemas),
        "assistant_event_count": len(assistant_events),
        "assistant_content_event_count": len(assistant_content_events),
        "assistant_content_replayable": assistant_content_replayable,
        "llm_call_event_count": len(llm_call_events),
        "llm_call_message_index_count": len(llm_call_events_with_message_indexes),
        "llm_call_messages_replayable": llm_call_messages_replayable,
        "llm_call_message_index_coverage": (
            len(llm_call_events_with_message_indexes) / len(llm_call_events)
            if llm_call_events
            else None
        ),
        "llm_call_tool_event_count": len(llm_call_tool_events),
        "llm_call_tool_schema_index_count": len(
            llm_call_events_with_tool_schema_indexes
        ),
        "llm_call_tool_schemas_replayable": llm_call_tool_schemas_replayable,
        "llm_call_tool_schema_index_coverage": (
            len(llm_call_events_with_tool_schema_indexes) / len(llm_call_tool_events)
            if llm_call_tool_events
            else None
        ),
        "tool_event_count": len(tool_events),
        "tool_result_event_count": len(tool_result_events),
        "tool_result_message_count": tool_message_count,
        "tool_result_replayable": tool_result_replayable,
        "tool_envelope_count": len(tool_events_with_envelope),
        "tool_envelope_coverage": (
            len(tool_events_with_envelope) / len(tool_events) if tool_events else None
        ),
        "blocking_gaps": blocking_gaps,
    }


def _sum_event_field(events: Iterable[Mapping[str, Any]], field: str) -> int:
    total = 0
    for event in events:
        value = _event_data(event).get(field)
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total


def _max_event_field(events: Iterable[Mapping[str, Any]], field: str) -> Optional[int]:
    values: List[int] = []
    for event in events:
        value = _event_data(event).get(field)
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            continue
    return max(values) if values else None


def _metric_recall_at_5(cell: Mapping[str, Any]) -> Optional[float]:
    metrics = _mapping(cell.get("metrics"))
    answer_blocks = _mapping(metrics.get("answer_blocks"))
    at_5 = _mapping(answer_blocks.get("5") or answer_blocks.get(5))
    value = at_5.get("recall")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").lstrip("./")


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _context_evidence_span(entry: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    state = str(entry.get("state") or "")
    if state not in {"read", "verified"}:
        return None
    file = _norm_path(entry.get("path"))
    if not file:
        return None
    metadata = _mapping(entry.get("metadata"))
    start = _int_or_none(
        metadata.get("start_line") or metadata.get("start") or metadata.get("offset")
    )
    end = _int_or_none(metadata.get("end_line") or metadata.get("end"))
    if start is None:
        return None
    if end is None:
        limit = _int_or_none(metadata.get("limit"))
        end = start + limit - 1 if limit else start
    if end < start:
        start, end = end, start
    return {
        "source": entry.get("source"),
        "state": state,
        "file": file,
        "start": start,
        "end": end,
        "tool_call_id": entry.get("tool_call_id"),
        "read_confirmed": True,
        "rank": metadata.get("rank"),
        "read_turn": metadata.get("read_turn"),
    }


def _scheduled_location_span(
    event: Mapping[str, Any], location: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    data = _event_data(event)
    file = _norm_path(location.get("file"))
    start = _int_or_none(location.get("start_line") or location.get("start"))
    end = _int_or_none(location.get("end_line") or location.get("end"))
    if not file or start is None:
        return None
    if end is None:
        end = start
    if end < start:
        start, end = end, start
    return {
        "source": data.get("source") or "scheduled_context",
        "state": "offered",
        "file": file,
        "start": start,
        "end": end,
        "tool_call_id": None,
        "read_confirmed": False,
        "turn": _event_turn(event),
        "operation": data.get("operation"),
        "role": location.get("role"),
        "name": location.get("name"),
        "location_source": location.get("source"),
    }


def _answer_span(span: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    file = _norm_path(span.get("file"))
    start = _int_or_none(span.get("start"))
    end = _int_or_none(span.get("end"))
    if not file or start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    return {"file": file, "start": start, "end": end}


def _spans_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        left.get("file") == right.get("file")
        and int(left["start"]) <= int(right["end"])
        and int(right["start"]) <= int(left["end"])
    )


def trace_evidence_spans(
    trace_payload: Optional[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Evidence spans from read-confirmed context and scheduled graph routes."""

    trace = _trace_from_payload(_mapping(trace_payload))
    spans: List[Dict[str, Any]] = []
    for entry in _context(trace):
        span = _context_evidence_span(entry)
        if span is not None:
            spans.append(span)
    for event in _events(trace):
        if _event_kind(event) != "scheduled_context":
            continue
        for location in _event_data(event).get("locations") or []:
            if not isinstance(location, Mapping):
                continue
            span = _scheduled_location_span(event, location)
            if span is not None:
                spans.append(span)
    return spans


def link_answer_spans_to_trace(
    answer_spans: Iterable[Mapping[str, Any]],
    trace_payload: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach each committed answer span to trace and read-confirmed evidence."""

    evidence_spans = trace_evidence_spans(trace_payload)
    linked: List[Dict[str, Any]] = []
    for index, raw_span in enumerate(answer_spans, start=1):
        span = _answer_span(raw_span)
        if span is None:
            continue
        evidence = [ev for ev in evidence_spans if _spans_overlap(span, ev)]
        read_evidence = [ev for ev in evidence if ev.get("read_confirmed")]
        linked.append(
            {
                "answer_index": index,
                "span": span,
                "trace_evidenced": bool(evidence),
                "read_confirmed": bool(read_evidence),
                "evidence_count": len(evidence),
                "read_evidence_count": len(read_evidence),
                "evidence": evidence,
            }
        )
    return linked


def summarize_answer_evidence(cell: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarize answer-span links back to trace/read-confirmed evidence."""

    links = cell.get("answer_span_evidence")
    raw_answer_spans = [
        span for span in cell.get("answer_spans") or [] if isinstance(span, Mapping)
    ]
    if not isinstance(links, list):
        links = link_answer_spans_to_trace(
            raw_answer_spans,
            _mapping(cell.get("trace")),
        )
    span_sources = {
        idx: str(span.get("source") or "")
        for idx, span in enumerate(raw_answer_spans, start=1)
    }
    explicit_links = [
        link
        for link in links
        if span_sources.get(_int_or_none(_mapping(link).get("answer_index")) or 0)
        != "answer_symbol"
    ]
    ignored_symbol_span_count = len(links) - len(explicit_links)
    if explicit_links:
        links = explicit_links
    trace_evidenced = [link for link in links if _mapping(link).get("trace_evidenced")]
    read_confirmed = [link for link in links if _mapping(link).get("read_confirmed")]
    unconfirmed = [
        _mapping(link).get("span")
        for link in links
        if not _mapping(link).get("trace_evidenced")
    ]
    span_count = len(links)
    return {
        "span_count": span_count,
        "trace_evidenced_count": len(trace_evidenced),
        "trace_evidenced_ratio": (
            len(trace_evidenced) / span_count if span_count else None
        ),
        "read_confirmed_count": len(read_confirmed),
        "read_confirmed_ratio": (
            len(read_confirmed) / span_count if span_count else None
        ),
        "unconfirmed_spans": unconfirmed,
        "ignored_symbol_span_count": ignored_symbol_span_count,
    }


def summarize_trace(trace_payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a compact, JSON-serializable summary for a trace payload."""

    trace = _trace_from_payload(_mapping(trace_payload))
    events = _events(trace)
    context = _context(trace)
    event_kinds = Counter(_event_kind(event) for event in events if _event_kind(event))

    run_start = next(
        (event for event in events if _event_kind(event) == "run_start"), None
    )
    run_end = next(
        (event for event in reversed(events) if _event_kind(event) == "run_end"),
        None,
    )
    max_turn = max(
        [turn for turn in (_event_turn(event) for event in events) if turn is not None],
        default=None,
    )

    tool_events = [event for event in events if _event_kind(event) == "tool_call"]
    tool_counts = Counter(
        str(_event_data(event).get("tool") or "unknown") for event in tool_events
    )
    read_events = [
        event for event in tool_events if str(_event_data(event).get("tool")) == "read"
    ]
    read_paths = _unique(
        _mapping(_event_data(event).get("arguments")).get("file_path")
        or _mapping(_event_data(event).get("arguments")).get("path")
        for event in read_events
    )
    tool_errors = sum(1 for event in tool_events if _event_data(event).get("error"))
    duplicate_tools = sum(
        1 for event in tool_events if bool(_event_data(event).get("duplicate"))
    )

    context_states = Counter(str(entry.get("state") or "unknown") for entry in context)
    context_sources = Counter(
        str(entry.get("source") or "unknown") for entry in context
    )
    context_source_states = Counter(
        f"{str(entry.get('source') or 'unknown')}:{str(entry.get('state') or 'unknown')}"
        for entry in context
    )
    context_read_paths = _unique(
        entry.get("path")
        for entry in context
        if str(entry.get("state") or "") in {"read", "verified"}
    )

    compaction_events = [
        event for event in events if _event_kind(event) == "compaction"
    ]
    scheduled_events = [
        event for event in events if _event_kind(event) == "scheduled_context"
    ]
    route_events = [
        event
        for event in scheduled_events
        if _event_data(event).get("operation") == "lsp_route"
    ]
    route_roles = Counter(
        role
        for event in route_events
        for role in (_event_data(event).get("route_roles") or [])
    )

    audit_kinds = (
        "fallback",
        "graph_expansion",
        "scheduled_anchor_audit",
        "scheduled_answer_ordering_audit",
        "scheduled_context_error",
        "scheduled_top_anchor_audit",
    )
    audits = {kind: int(event_kinds.get(kind, 0)) for kind in audit_kinds}

    run_end_data = _event_data(run_end or {})
    run_start_data = _event_data(run_start or {})
    return {
        "summary_version": TRACE_SUMMARY_VERSION,
        "trace_schema_version": trace.get("schema_version"),
        "has_trace": bool(events or context),
        "has_run_bounds": bool(run_start and run_end),
        "events": {
            "count": len(events),
            "kinds": _sorted_counter(event_kinds),
        },
        "turns": {
            "max_observed": max_turn,
            "run_end": _event_turn(run_end or {}) if run_end else None,
        },
        "run": {
            "max_turns": run_start_data.get("max_turns"),
            "tool_count_available": run_start_data.get("tool_count"),
            "query_chars": run_start_data.get("query_chars"),
            "preload_candidates": run_start_data.get("preload_candidates"),
            "answer_chars": run_end_data.get("answer_chars"),
            "duration_ms": run_end_data.get("duration_ms"),
        },
        "tools": {
            "count": len(tool_events),
            "by_name": _sorted_counter(tool_counts),
            "errors": tool_errors,
            "duplicates": duplicate_tools,
            "first_turn": _first_turn(tool_events),
        },
        "reads": {
            "count": len(read_events),
            "first_turn": _first_turn(read_events),
            "unique_paths": read_paths,
            "unique_path_count": len(read_paths),
        },
        "context": {
            "count": len(context),
            "states": _sorted_counter(context_states),
            "sources": _sorted_counter(context_sources),
            "source_states": _sorted_counter(context_source_states),
            "read_or_verified_paths": context_read_paths,
            "read_or_verified_path_count": len(context_read_paths),
        },
        "compaction": {
            "count": len(compaction_events),
            "first_turn": _first_turn(compaction_events),
            "seed_chars": _sum_event_field(compaction_events, "seed_chars"),
            "protected_tail_chars": _sum_event_field(
                compaction_events, "protected_tail_chars"
            ),
            "original_tool_result_chars": _sum_event_field(
                compaction_events, "original_tool_result_chars"
            ),
            "pruned_tool_result_chars": _sum_event_field(
                compaction_events, "pruned_tool_result_chars"
            ),
        },
        "scheduled_context": {
            "count": len(scheduled_events),
            "first_turn": _first_turn(scheduled_events),
            "operations": _unique(
                _event_data(event).get("operation") for event in scheduled_events
            ),
            "reasons": _unique(
                _event_data(event).get("reason") for event in scheduled_events
            ),
            "lsp_route_count": len(route_events),
            "lsp_route_first_turn": _first_turn(route_events),
            "node_count_max": _max_event_field(scheduled_events, "node_count"),
            "route_read_confirmed_core_max": _max_event_field(
                route_events, "route_read_confirmed_core_count"
            ),
            "route_unread_core_max": _max_event_field(
                route_events, "route_unread_core_count"
            ),
            "route_roles": _sorted_counter(route_roles),
        },
        "audits": audits,
    }


def summarize_cell_trace(cell: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarize a persisted sweep cell, including trace and scored outcome."""

    summary = summarize_trace(_mapping(cell.get("trace")))
    summary["cell"] = {
        "cell_id": cell.get("cell_id"),
        "instance_id": cell.get("instance_id"),
        "query_id": cell.get("query_id"),
        "category": cell.get("category"),
        "subset_id": cell.get("subset_id"),
        "success": cell.get("success"),
        "format_failed": cell.get("format_failed"),
        "answer_span_count": cell.get("answer_span_count"),
        "answer_rec_at_5": _metric_recall_at_5(cell),
        "total_turns": cell.get("total_turns"),
        "total_tokens": cell.get("total_tokens"),
    }
    summary["answer_evidence"] = summarize_answer_evidence(cell)
    summary["replay_readiness"] = summarize_replay_readiness(cell)
    return summary


def _format_location(span: Mapping[str, Any]) -> str:
    file = span.get("file")
    start = span.get("start")
    end = span.get("end")
    if not file:
        return "n/a"
    if start is None:
        return str(file)
    if end is None or end == start:
        return f"{file}:{start}"
    return f"{file}:{start}-{end}"


def _tool_summary(data: Mapping[str, Any]) -> str:
    tool = data.get("tool") or "unknown"
    args = _mapping(data.get("arguments"))
    envelope = _mapping(data.get("envelope"))
    path = args.get("file_path") or args.get("path")
    location = ""
    if path:
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is not None and limit is not None:
            location = f" {_norm_path(path)}:{offset}+{limit}"
        else:
            location = f" {_norm_path(path)}"
    status = envelope.get("status") or ("error" if data.get("error") else "ok")
    result_chars = envelope.get("result_chars", data.get("result_chars"))
    result = f" result_chars={result_chars}" if result_chars is not None else ""
    error_kind = envelope.get("error_kind")
    error = f" error_kind={error_kind}" if error_kind else ""
    truncated = " truncated" if envelope.get("truncated") else ""
    duplicate = " duplicate" if data.get("duplicate") else ""
    metadata = _mapping(envelope.get("metadata"))
    policy_parts = []
    if metadata.get("exit_code") is not None:
        policy_parts.append(f"exit={metadata.get('exit_code')}")
    if metadata.get("permission_boundary"):
        policy_parts.append(f"perm={metadata.get('permission_boundary')}")
    if metadata.get("sandbox"):
        policy_parts.append(f"sandbox={metadata.get('sandbox')}")
    if metadata.get("side_effects_possible") is not None:
        policy_parts.append(
            f"sidefx={str(bool(metadata.get('side_effects_possible'))).lower()}"
        )
    policy = (" " + " ".join(policy_parts)) if policy_parts else ""
    return (
        f"tool {tool}{location} {status}{duplicate}{truncated}{error}{result}"
        f"{policy}".strip()
    )


def _scheduled_summary(data: Mapping[str, Any]) -> str:
    operation = data.get("operation") or "unknown"
    reason = data.get("reason") or "n/a"
    node_count = data.get("node_count")
    confirmed = data.get("route_read_confirmed_core_count")
    unread = data.get("route_unread_core_count")
    parts = [f"scheduled {operation}", f"reason={reason}"]
    if node_count is not None:
        parts.append(f"nodes={node_count}")
    if confirmed is not None:
        parts.append(f"confirmed_core={confirmed}")
    if unread is not None:
        parts.append(f"unread_core={unread}")
    roles = data.get("route_roles")
    if roles:
        parts.append(f"roles={','.join(str(role) for role in roles)}")
    return " ".join(parts)


def _event_replay_summary(event: Mapping[str, Any]) -> str:
    kind = _event_kind(event)
    data = _event_data(event)
    if kind == "run_start":
        return (
            "run_start "
            f"max_turns={data.get('max_turns')} "
            f"tools={data.get('tool_count')} "
            f"preload={data.get('preload_candidates')}"
        )
    if kind == "assistant":
        return (
            "assistant "
            f"content_chars={data.get('content_chars')} "
            f"tool_calls={data.get('tool_calls') or []}"
        )
    if kind == "tool_call":
        return _tool_summary(data)
    if kind == "compaction":
        return (
            "compaction "
            f"seed_chars={data.get('seed_chars')} "
            f"pruned_tool_chars={data.get('pruned_tool_result_chars')} "
            f"tail_chars={data.get('protected_tail_chars')}"
        )
    if kind == "scheduled_context":
        return _scheduled_summary(data)
    if kind == "fallback":
        return f"fallback strategy={data.get('strategy') or 'n/a'}"
    if kind == "graph_expansion":
        return (
            "graph_expansion "
            f"reason={data.get('reason')} "
            f"nodes={data.get('node_count')}"
        )
    if kind == "scheduled_anchor_audit":
        return (
            "scheduled_anchor_audit "
            f"offered={data.get('offered_count')} "
            f"read={data.get('read_count')} "
            f"cited={data.get('cited_count')}"
        )
    if kind == "scheduled_top_anchor_audit":
        return (
            "scheduled_top_anchor_audit "
            f"mode={data.get('correction_mode')} "
            f"missing={data.get('missing_count')} "
            f"missing_unread={data.get('missing_unread_count')}"
        )
    if kind == "scheduled_answer_ordering_audit":
        return (
            "scheduled_answer_ordering_audit "
            f"reason={data.get('reason')} "
            f"spans={data.get('answer_span_count')}"
        )
    if kind == "answer_path_alias_normalization":
        return (
            "answer_path_alias_normalization "
            f"replacements={data.get('replacement_count')}"
        )
    if kind == "answer_schema_salvage":
        return (
            "answer_schema_salvage "
            f"reason={data.get('reason')} "
            f"locations={data.get('location_count')}"
        )
    if kind == "answer_location_order_normalization":
        return (
            "answer_location_order_normalization "
            f"reason={data.get('reason')} "
            f"promotions={data.get('promotion_count')}"
        )
    if kind == "run_end":
        return (
            "run_end "
            f"answer_chars={data.get('answer_chars')} "
            f"tool_calls={data.get('tool_calls')} "
            f"duration_ms={data.get('duration_ms')}"
        )
    return kind or "event"


def build_cell_replay(cell: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a deterministic replay timeline from a persisted cell trace."""

    trace = _mapping(cell.get("trace"))
    timeline = []
    for sequence, event in enumerate(_events(trace), start=1):
        timeline.append(
            {
                "sequence": sequence,
                "turn": _event_turn(event),
                "kind": _event_kind(event),
                "summary": _event_replay_summary(event),
                "data": _event_data(event),
            }
        )
    answer_evidence = (
        cell.get("answer_span_evidence")
        if isinstance(cell.get("answer_span_evidence"), list)
        else link_answer_spans_to_trace(
            [
                span
                for span in cell.get("answer_spans") or []
                if isinstance(span, Mapping)
            ],
            trace,
        )
    )
    return {
        "replay_version": TRACE_REPLAY_VERSION,
        "cell": {
            "cell_id": cell.get("cell_id"),
            "instance_id": cell.get("instance_id"),
            "query_id": cell.get("query_id"),
            "category": cell.get("category"),
            "subset_id": cell.get("subset_id"),
            "success": cell.get("success"),
        },
        "summary": summarize_cell_trace(cell),
        "timeline": timeline,
        "context_ledger": _context(trace),
        "message_replay": _persisted_messages(cell),
        "tool_schema_replay": _persisted_tool_schemas(cell),
        "replay_readiness": summarize_replay_readiness(cell),
        "answer": {
            "text": cell.get("answer") or "",
            "spans": [
                span
                for span in cell.get("answer_spans") or []
                if isinstance(span, Mapping)
            ],
            "evidence": answer_evidence,
        },
    }


def load_cell_replay(path: Path) -> Dict[str, Any]:
    """Load a persisted cell JSON file and return ``build_cell_replay``."""

    cell = json.loads(path.read_text(encoding="utf-8"))
    return build_cell_replay(_mapping(cell))


def load_cell_trace_summary(path: Path) -> Dict[str, Any]:
    """Load a persisted cell JSON file and return ``summarize_cell_trace``."""

    cell = json.loads(path.read_text(encoding="utf-8"))
    return summarize_cell_trace(_mapping(cell))


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    """Render a short human-readable trace summary."""

    cell = _mapping(summary.get("cell"))
    tools = _mapping(summary.get("tools"))
    reads = _mapping(summary.get("reads"))
    scheduled = _mapping(summary.get("scheduled_context"))
    compaction = _mapping(summary.get("compaction"))
    audits = _mapping(summary.get("audits"))
    answer_evidence = _mapping(summary.get("answer_evidence"))
    replay_readiness = _mapping(summary.get("replay_readiness"))
    lines = [
        "# Agent Trace Summary",
        "",
        f"- cell: {cell.get('cell_id') or 'n/a'}",
        f"- success: {cell.get('success')}",
        f"- answer_rec@5: {cell.get('answer_rec_at_5')}",
        f"- turns: {cell.get('total_turns')}",
        f"- tools: {tools.get('count')} ({tools.get('by_name')})",
        f"- reads: {reads.get('count')} ({reads.get('unique_paths')})",
        (
            "- answer_evidence: "
            f"{answer_evidence.get('trace_evidenced_count')}/"
            f"{answer_evidence.get('span_count')} trace-evidenced, "
            f"{answer_evidence.get('read_confirmed_count')}/"
            f"{answer_evidence.get('span_count')} read-confirmed"
        ),
        (
            "- replay_readiness: "
            f"event={replay_readiness.get('event_replayable')} "
            f"exact={replay_readiness.get('exact_message_replayable')} "
            f"gaps={replay_readiness.get('blocking_gaps')}"
        ),
        (
            "- scheduled_context: "
            f"{scheduled.get('count')} operations={scheduled.get('operations')} "
            f"lsp_route={scheduled.get('lsp_route_count')}"
        ),
        f"- compaction: {compaction.get('count')}",
        f"- audits: {audits}",
    ]
    return "\n".join(lines) + "\n"


def render_replay_markdown(replay: Mapping[str, Any]) -> str:
    """Render a replay timeline as compact Markdown."""

    cell = _mapping(replay.get("cell"))
    summary = _mapping(replay.get("summary"))
    answer_evidence = _mapping(summary.get("answer_evidence"))
    replay_readiness = _mapping(replay.get("replay_readiness"))
    answer = _mapping(replay.get("answer"))
    lines = [
        "# Agent Trace Replay",
        "",
        f"- cell: {cell.get('cell_id') or 'n/a'}",
        f"- success: {cell.get('success')}",
        (
            "- answer_evidence: "
            f"{answer_evidence.get('trace_evidenced_count')}/"
            f"{answer_evidence.get('span_count')} trace-evidenced, "
            f"{answer_evidence.get('read_confirmed_count')}/"
            f"{answer_evidence.get('span_count')} read-confirmed"
        ),
        (
            "- replay_readiness: "
            f"event={replay_readiness.get('event_replayable')} "
            f"exact={replay_readiness.get('exact_message_replayable')} "
            f"gaps={replay_readiness.get('blocking_gaps')}"
        ),
        "",
        "## Timeline",
        "",
    ]
    for item in replay.get("timeline") or []:
        if not isinstance(item, Mapping):
            continue
        turn = item.get("turn")
        prefix = f"T{turn}" if turn is not None else "T?"
        lines.append(
            f"{int(item.get('sequence') or 0):02d}. `{prefix}` "
            f"`{item.get('kind')}` {item.get('summary')}"
        )
    spans = [
        _format_location(_mapping(span))
        for span in answer.get("spans") or []
        if isinstance(span, Mapping)
    ]
    if spans:
        lines.extend(["", "## Answer Spans", ""])
        for span in spans:
            lines.append(f"- {span}")
    text = str(answer.get("text") or "").strip()
    if text:
        preview = text[:800]
        if len(text) > len(preview):
            preview += "\n..."
        lines.extend(["", "## Answer", "", "```text", preview, "```"])
    return "\n".join(lines) + "\n"
