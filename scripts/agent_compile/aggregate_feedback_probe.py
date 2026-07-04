#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate a small trace-aware feedback probe for agent harness iteration.

This is intentionally smaller and more operational than the paper-style
aggregates. It answers: did the harness change improve committed localization,
and did the trace show the expected mechanism (fewer turns/tokens, successful
reads, compaction firing, fewer rejected/dead-end context items)?
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

BASELINE = "grep_only"
ROUTE_LIFECYCLE_ARM = "preinj_graph_scheduled"
EPS = 0.02
ROUTE_LIFECYCLE_MIN_QUERIES = 4
ROUTE_LIFECYCLE_MIN_REPS = 2.0
PROMOTION_PROFILES: Dict[str, Dict[str, Any]] = {
    "haiku_compact": {
        "description": (
            "Promote the default Haiku compact arm only when it preserves "
            "recall, saves tokens or turns, and every committed answer span is "
            "trace-evidenced."
        ),
        "feedback_arms": ["preinj_eager_compact"],
        "feedback_require_savings": True,
        "answer_evidence_arms": ["preinj_eager_compact"],
        "answer_evidence_min_trace_rate": 1.0,
        "answer_evidence_min_read_rate": None,
        "answer_evidence_max_unconfirmed": 0.0,
        "replay_arms": ["preinj_eager_compact"],
    },
    "route_lifecycle": {
        "description": (
            "Promote the scheduled graph/LSP route lifecycle only when the "
            "repeat gate passes and final answers remain trace-evidenced."
        ),
        "route_lifecycle": True,
        "answer_evidence_arms": [ROUTE_LIFECYCLE_ARM],
        "answer_evidence_min_trace_rate": 1.0,
        "answer_evidence_min_read_rate": None,
        "answer_evidence_max_unconfirmed": 0.0,
        "replay_arms": [ROUTE_LIFECYCLE_ARM],
    },
}
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


def _load(cells_dir: Path) -> List[Dict[str, Any]]:
    cells = []
    for p in sorted(cells_dir.glob("*.json")):
        try:
            cell = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if cell.get("success"):
            cells.append(cell)
    return cells


def _metric(
    cell: Dict[str, Any],
    scope: str = "answer_blocks",
    k: int = 5,
    field: str = "recall",
) -> Optional[float]:
    bucket = ((cell.get("metrics") or {}).get(scope) or {}).get(k)
    if bucket is None:
        bucket = ((cell.get("metrics") or {}).get(scope) or {}).get(str(k))
    return bucket.get(field) if isinstance(bucket, dict) else None


def _mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return statistics.fmean(vals) if vals else None


def _min(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return min(vals) if vals else None


def _max(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return max(vals) if vals else None


def _ratio(numer: Optional[float], denom: Optional[float]) -> Optional[float]:
    if numer is None or denom is None or denom <= 0:
        return None
    return numer / denom


def _fmt(v: Optional[float], nd: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


def _answer_diagnosis(cell: Dict[str, Any]) -> Optional[str]:
    from scripts.agent_compile.lib.answer_diagnostics import answer_diagnosis_from_cell

    return answer_diagnosis_from_cell(cell)


def _summarize_answer_evidence(cell: Dict[str, Any]) -> Dict[str, Any]:
    from scripts.agent_compile.lib.trace_summary import summarize_answer_evidence

    return summarize_answer_evidence(cell)


def _summarize_replay_readiness(cell: Dict[str, Any]) -> Dict[str, Any]:
    from scripts.agent_compile.lib.trace_summary import summarize_replay_readiness

    return summarize_replay_readiness(cell)


def _answer_diagnosis_rate(
    rows: Sequence[Dict[str, Any]], label: str
) -> Optional[float]:
    diagnoses = [_answer_diagnosis(row) for row in rows]
    known = [diagnosis for diagnosis in diagnoses if diagnosis]
    if not known:
        return None
    return statistics.fmean(1.0 if diagnosis == label else 0.0 for diagnosis in known)


def _answer_diagnosis_mix(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    for label in ANSWER_DIAGNOSES:
        rate = row.get(f"answer_diag_{label}")
        if rate is None or rate <= 0:
            continue
        parts.append(f"{ANSWER_DIAGNOSIS_SHORT[label]}:{100 * rate:.0f}%")
    return ",".join(parts) if parts else "n/a"


def _recall_from_metrics(metrics: Any) -> Optional[float]:
    if not isinstance(metrics, dict):
        return None
    try:
        return float(metrics.get("recall"))
    except (TypeError, ValueError):
        return None


def _answer_quality_fields(cell: Dict[str, Any]) -> Dict[str, Optional[float]]:
    rec_all = _recall_from_metrics(cell.get("answer_all_metrics"))
    alias_rec_all = _recall_from_metrics(cell.get("answer_path_alias_metrics"))
    if rec_all is not None and alias_rec_all is not None:
        return {
            "answer_rec_all": rec_all,
            "answer_path_alias_rec_all": alias_rec_all,
        }

    spans = cell.get("answer_spans")
    gt_blocks = cell.get("gt_code_blocks") or cell.get("code_blocks") or []
    if isinstance(spans, list) and isinstance(gt_blocks, list) and (spans or gt_blocks):
        from scripts.agent_compile.lib.answer_diagnostics import (
            build_answer_diagnostic_fields,
        )

        metrics = cell.get("metrics") if isinstance(cell.get("metrics"), dict) else {}
        _deduped, fields = build_answer_diagnostic_fields(
            answer=str(cell.get("answer") or ""),
            answer_spans=[span for span in spans if isinstance(span, dict)],
            gt_blocks=[block for block in gt_blocks if isinstance(block, dict)],
            metrics=metrics,
            metrics_k=[5],
        )
        rec_all = (
            rec_all
            if rec_all is not None
            else _recall_from_metrics(fields.get("answer_all_metrics"))
        )
        alias_rec_all = (
            alias_rec_all
            if alias_rec_all is not None
            else _recall_from_metrics(fields.get("answer_path_alias_metrics"))
        )

    return {
        "answer_rec_all": rec_all,
        "answer_path_alias_rec_all": alias_rec_all,
    }


def _tool_event_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    envelope = data.get("envelope")
    return envelope if isinstance(envelope, dict) else {}


def _tool_event_status(data: Dict[str, Any]) -> str:
    envelope = _tool_event_envelope(data)
    status = envelope.get("status")
    if status:
        return str(status)
    return "error" if data.get("error") else "ok"


def _tool_event_result_count(data: Dict[str, Any]) -> Optional[float]:
    envelope = _tool_event_envelope(data)
    value = envelope.get("result_count")
    if value is None:
        value = data.get("n_results")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tool_event_result_chars(data: Dict[str, Any]) -> Optional[float]:
    envelope = _tool_event_envelope(data)
    value = envelope.get("result_chars")
    if value is None:
        value = data.get("result_chars")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tool_event_truncated(data: Dict[str, Any]) -> bool:
    envelope = _tool_event_envelope(data)
    metadata = (
        envelope.get("metadata") if isinstance(envelope.get("metadata"), dict) else {}
    )
    return bool(
        envelope.get("truncated")
        or metadata.get("output_truncated")
        or data.get("truncated")
    )


def _tool_event_stats(events: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    tool_events = [event for event in events if event.get("kind") == "tool_call"]
    if not tool_events:
        return {
            "tool_calls": 0.0,
            "tool_envelope_coverage": None,
            "tool_errors": 0.0,
            "tool_skipped": 0.0,
            "tool_truncated": 0.0,
            "tool_result_chars": 0.0,
        }
    data_rows = [event.get("data") or {} for event in tool_events]
    envelope_count = sum(1.0 for data in data_rows if _tool_event_envelope(data))
    result_chars = [
        value
        for data in data_rows
        if (value := _tool_event_result_chars(data)) is not None
    ]
    return {
        "tool_calls": float(len(tool_events)),
        "tool_envelope_coverage": envelope_count / len(tool_events),
        "tool_errors": float(
            sum(1 for data in data_rows if _tool_event_status(data) == "error")
        ),
        "tool_skipped": float(
            sum(1 for data in data_rows if _tool_event_status(data) == "skipped")
        ),
        "tool_truncated": float(
            sum(1 for data in data_rows if _tool_event_truncated(data))
        ),
        "tool_result_chars": float(sum(result_chars)),
    }


def _read_window(read: Dict[str, Any]) -> Optional[Tuple[str, int, int]]:
    path = read.get("file_path") or read.get("path")
    if not path:
        return None
    try:
        start = max(1, int(read.get("offset") or 1))
        limit = max(1, int(read.get("limit") or 200))
    except (TypeError, ValueError):
        return None
    return str(path), start, start + limit - 1


def _trace_read_records(cell: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    events = (cell.get("trace") or {}).get("events") or []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "tool_call":
            continue
        data = event.get("data") or {}
        if str(data.get("tool") or "") != "read" or data.get("error"):
            continue
        args = data.get("arguments") or {}
        path = args.get("file_path") or args.get("path")
        if not path:
            continue
        record = {
            "file_path": str(path),
            "offset": args.get("offset"),
            "limit": args.get("limit"),
        }
        tool_call_id = data.get("tool_call_id")
        if tool_call_id:
            record["tool_call_id"] = str(tool_call_id)
        turn = _event_turn(event)
        if turn is not None:
            record["turn"] = turn
        records.append(record)
    return records


def _read_records(cell: Dict[str, Any]) -> List[Dict[str, Any]]:
    file_reads = [r for r in cell.get("file_reads") or [] if isinstance(r, dict)]
    if file_reads and all(_read_turn(r) is not None for r in file_reads):
        return file_reads
    trace_reads = _trace_read_records(cell)
    return trace_reads or file_reads


def _event_turn(event: Dict[str, Any]) -> Optional[int]:
    try:
        return int(event.get("turn"))
    except (TypeError, ValueError):
        return None


def _read_turn(read: Dict[str, Any]) -> Optional[int]:
    try:
        return int(read.get("turn"))
    except (TypeError, ValueError):
        return None


def _first_scheduled_operation_turn(
    cell: Dict[str, Any], operation: str
) -> Optional[int]:
    events = (cell.get("trace") or {}).get("events") or []
    turns = []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "scheduled_context":
            continue
        data = event.get("data") or {}
        if str(data.get("operation") or "").strip() != operation:
            continue
        turn = _event_turn(event)
        if turn is not None:
            turns.append(turn)
    return min(turns) if turns else None


def _line_overlap(start: int, end: int, ranges: Sequence[Tuple[int, int]]) -> int:
    total = 0
    for prev_start, prev_end in ranges:
        total += max(0, min(end, prev_end) - max(start, prev_start) + 1)
    return total


def _read_overlap_kind(
    start: int, end: int, ranges: Sequence[Tuple[int, int]], repeated_start: bool
) -> str:
    if any(prev_start <= start and end <= prev_end for prev_start, prev_end in ranges):
        return "contained"
    if repeated_start:
        return "expansion"
    return "shifted"


def _merge_range(ranges: List[Tuple[int, int]], start: int, end: int) -> None:
    ranges.append((start, end))
    ranges.sort()
    merged: List[Tuple[int, int]] = []
    for cur_start, cur_end in ranges:
        if not merged or cur_start > merged[-1][1] + 1:
            merged.append((cur_start, cur_end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], cur_end))
    ranges[:] = merged


def _read_overlap_stats(cell: Dict[str, Any]) -> Dict[str, Optional[float]]:
    ranges_by_file: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
    seen_offsets: set[Tuple[str, int]] = set()
    route_turn = _first_scheduled_operation_turn(cell, "lsp_route")
    guard_turn = _first_scheduled_operation_turn(cell, "lsp_route_finalization_guard")
    requested_lines = 0
    overlap_lines = 0
    overlap_events = 0
    overlap_pre_route_lines = 0
    overlap_pre_route_events = 0
    overlap_post_route_lines = 0
    overlap_post_route_events = 0
    overlap_post_guard_lines = 0
    overlap_post_guard_events = 0
    overlap_unknown_phase_lines = 0
    overlap_unknown_phase_events = 0
    overlap_low_novelty_events = 0
    overlap_post_route_low_novelty_events = 0
    overlap_post_route_contained_events = 0
    overlap_post_route_expansion_events = 0
    overlap_post_route_shifted_events = 0
    repeat_offsets = 0
    for read in _read_records(cell):
        window = _read_window(read)
        if window is None:
            continue
        path, start, end = window
        read_lines = end - start + 1
        requested_lines += read_lines
        key = (path, start)
        repeated_start = key in seen_offsets
        if key in seen_offsets:
            repeat_offsets += 1
        seen_offsets.add(key)
        ranges = ranges_by_file[path]
        overlap = _line_overlap(start, end, ranges)
        if overlap:
            overlap_events += 1
            overlap_lines += overlap
            kind = _read_overlap_kind(start, end, ranges, repeated_start)
            low_novelty = overlap / read_lines >= 0.8 if read_lines else False
            if low_novelty:
                overlap_low_novelty_events += 1
            turn = _read_turn(read)
            if turn is None:
                overlap_unknown_phase_events += 1
                overlap_unknown_phase_lines += overlap
            elif route_turn is None or turn <= route_turn:
                overlap_pre_route_events += 1
                overlap_pre_route_lines += overlap
            else:
                overlap_post_route_events += 1
                overlap_post_route_lines += overlap
                if kind == "contained":
                    overlap_post_route_contained_events += 1
                elif kind == "expansion":
                    overlap_post_route_expansion_events += 1
                else:
                    overlap_post_route_shifted_events += 1
                if low_novelty:
                    overlap_post_route_low_novelty_events += 1
                if guard_turn is not None and turn > guard_turn:
                    overlap_post_guard_events += 1
                    overlap_post_guard_lines += overlap
        _merge_range(ranges, start, end)
    return {
        "read_repeat_offsets": float(repeat_offsets),
        "read_overlap_events": float(overlap_events),
        "read_overlap_lines": float(overlap_lines),
        "read_overlap_rate": (
            overlap_lines / requested_lines if requested_lines else None
        ),
        "read_overlap_pre_route_events": float(overlap_pre_route_events),
        "read_overlap_pre_route_lines": float(overlap_pre_route_lines),
        "read_overlap_post_route_events": float(overlap_post_route_events),
        "read_overlap_post_route_lines": float(overlap_post_route_lines),
        "read_overlap_post_route_contained_events": float(
            overlap_post_route_contained_events
        ),
        "read_overlap_post_route_expansion_events": float(
            overlap_post_route_expansion_events
        ),
        "read_overlap_post_route_shifted_events": float(
            overlap_post_route_shifted_events
        ),
        "read_overlap_low_novelty_events": float(overlap_low_novelty_events),
        "read_overlap_post_route_low_novelty_events": float(
            overlap_post_route_low_novelty_events
        ),
        "read_overlap_post_guard_events": float(overlap_post_guard_events),
        "read_overlap_post_guard_lines": float(overlap_post_guard_lines),
        "read_overlap_unknown_phase_events": float(overlap_unknown_phase_events),
        "read_overlap_unknown_phase_lines": float(overlap_unknown_phase_lines),
    }


def _span_overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    return max(start, other_start) <= min(end, other_end)


def _route_location_window(loc: Dict[str, Any]) -> Optional[Tuple[str, int, int]]:
    from codeminer.eval.retrieval_eval import normalize_file_path

    path = normalize_file_path(loc.get("file") or loc.get("file_path"))
    if not path:
        return None
    try:
        start = int(loc.get("start_line") or loc.get("start") or 0)
        end = int(loc.get("end_line") or loc.get("end") or start)
    except (TypeError, ValueError):
        return None
    if start <= 0 or end < start:
        return None
    return path, start, end


def _route_location_key(loc: Dict[str, Any]) -> Tuple[str, int, int, str]:
    window = _route_location_window(loc)
    if window is None:
        return ("", 0, 0, str(loc.get("name") or ""))
    path, start, end = window
    return path, start, end, str(loc.get("name") or "")


def _route_leaf(value: Any) -> str:
    text = str(value or "").strip().strip("`'\"").rstrip("()")
    for sep in ("#", "::", ".", ":"):
        text = text.split(sep)[-1]
    return text.strip()


def _route_protected_core_keys(
    locations: Sequence[Dict[str, Any]],
) -> set[Tuple[str, int, int, str]]:
    via_leafs = {
        _route_leaf(loc.get("via")).lower()
        for loc in locations or []
        if loc.get("source") == "route_neighbor" and loc.get("via")
    }
    keep: set[Tuple[str, int, int, str]] = set()
    for loc in locations or []:
        role = str(loc.get("role") or "support")
        source = str(loc.get("source") or "")
        leaf = _route_leaf(loc.get("name")).lower()
        if (
            source == "route_neighbor"
            or role in {"bridge", "type"}
            or (
                source == "direct_definition"
                and role in {"bridge", "provider", "type"}
                and leaf in via_leafs
            )
        ):
            keep.add(_route_location_key(loc))
    return keep


def _route_read_ranges_before_turn(
    cell: Dict[str, Any],
    route_turn: Optional[int],
) -> List[Tuple[str, int, int]]:
    from codeminer.eval.retrieval_eval import normalize_file_path

    ranges: List[Tuple[str, int, int]] = []
    for read in _read_records(cell):
        turn = _read_turn(read)
        if route_turn is not None and turn is not None and turn > route_turn:
            continue
        window = _read_window(read)
        if window is None:
            continue
        path, start, end = window
        normalized = normalize_file_path(path)
        if normalized:
            ranges.append((normalized, start, end))
    return ranges


def _reconstruct_route_read_memory_details(
    cell: Dict[str, Any],
    event: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    read_ranges = _route_read_ranges_before_turn(cell, _event_turn(event))
    data = event.get("data") or {}
    for loc in data.get("auto_open_locations") or []:
        window = _route_location_window(loc)
        if window is not None:
            read_ranges.append(window)

    confirmed: List[Dict[str, Any]] = []
    locations = [
        loc
        for loc in (data.get("locations") or data.get("route_locations") or [])
        if isinstance(loc, dict)
    ]
    for loc in locations:
        window = _route_location_window(loc)
        if window is None:
            continue
        path, start, end = window
        if any(
            read_path == path and _span_overlaps(start, end, rstart, rend)
            for read_path, rstart, rend in read_ranges
        ):
            confirmed.append(loc)

    confirmed_keys = {_route_location_key(loc) for loc in confirmed}
    core_roles = {"endpoint", "bridge", "provider", "type"}
    confirmed_core = [
        loc for loc in confirmed if str(loc.get("role") or "") in core_roles
    ]
    unread_core = [
        loc
        for loc in locations
        if _route_location_key(loc) not in confirmed_keys
        and str(loc.get("role") or "") in core_roles
    ]
    return confirmed, confirmed_core, unread_core


def _reconstruct_route_read_memory(
    cell: Dict[str, Any],
    event: Dict[str, Any],
) -> Tuple[float, float, float]:
    confirmed, confirmed_core, unread_core = _reconstruct_route_read_memory_details(
        cell,
        event,
    )
    return float(len(confirmed)), float(len(confirmed_core)), float(len(unread_core))


def _route_read_memory_stats(
    cell: Dict[str, Any],
    scheduled_route_events: Sequence[Dict[str, Any]],
) -> Tuple[float, float, float, float, float]:
    confirmed = 0.0
    confirmed_core = 0.0
    unread_core = 0.0
    confirmed_protected = 0.0
    unread_protected = 0.0
    metric_keys = {
        "route_read_confirmed_count",
        "route_read_confirmed_core_count",
        "route_unread_core_count",
    }
    for event in scheduled_route_events:
        data = event.get("data") or {}
        if metric_keys & data.keys():
            confirmed += float(data.get("route_read_confirmed_count") or 0)
            confirmed_core += float(data.get("route_read_confirmed_core_count") or 0)
            unread_core += float(data.get("route_unread_core_count") or 0)
            if (
                "route_read_confirmed_protected_count" in data
                or "route_unread_protected_count" in data
            ):
                confirmed_protected += float(
                    data.get("route_read_confirmed_protected_count") or 0
                )
                unread_protected += float(data.get("route_unread_protected_count") or 0)
            else:
                locations = [
                    loc
                    for loc in (
                        data.get("locations") or data.get("route_locations") or []
                    )
                    if isinstance(loc, dict)
                ]
                protected_keys = _route_protected_core_keys(locations)
                if protected_keys:
                    (
                        event_confirmed_details,
                        _event_core,
                        _event_unread_core,
                    ) = _reconstruct_route_read_memory_details(cell, event)
                    confirmed_keys = {
                        _route_location_key(loc) for loc in event_confirmed_details
                    }
                    confirmed_protected += float(len(protected_keys & confirmed_keys))
                    unread_protected += float(len(protected_keys - confirmed_keys))
            continue
        (
            event_confirmed,
            event_confirmed_core,
            event_unread_core,
        ) = _reconstruct_route_read_memory(cell, event)
        confirmed += event_confirmed
        confirmed_core += event_confirmed_core
        unread_core += event_unread_core
        locations = [
            loc
            for loc in (
                (event.get("data") or {}).get("locations")
                or (event.get("data") or {}).get("route_locations")
                or []
            )
            if isinstance(loc, dict)
        ]
        protected_keys = _route_protected_core_keys(locations)
        if protected_keys:
            (
                event_confirmed_details,
                _event_core,
                _event_unread_core,
            ) = _reconstruct_route_read_memory_details(cell, event)
            confirmed_keys = {
                _route_location_key(loc) for loc in event_confirmed_details
            }
            confirmed_protected += float(len(protected_keys & confirmed_keys))
            unread_protected += float(len(protected_keys - confirmed_keys))
    return (
        confirmed,
        confirmed_core,
        unread_core,
        confirmed_protected,
        unread_protected,
    )


def _route_unread_core_locations(
    cell: Dict[str, Any],
    event: Dict[str, Any],
) -> List[Dict[str, Any]]:
    data = event.get("data") or {}
    if data.get("locations") or data.get("route_locations"):
        (
            _confirmed,
            _confirmed_core,
            unread_core,
        ) = _reconstruct_route_read_memory_details(cell, event)
        return unread_core
    return [
        loc
        for loc in data.get("route_unread_core_locations") or []
        if isinstance(loc, dict)
    ]


def _answer_span_windows(cell: Dict[str, Any]) -> List[Tuple[str, int, int, int]]:
    from codeminer.eval.retrieval_eval import parse_answer_spans

    windows: List[Tuple[str, int, int, int]] = []
    for rank, span in enumerate(parse_answer_spans(cell.get("answer") or "", None), 1):
        window = _route_location_window(span)
        if window is None:
            continue
        path, start, end = window
        windows.append((path, start, end, rank))
    return windows


def _route_unread_answer_stats(
    cell: Dict[str, Any],
    scheduled_route_events: Sequence[Dict[str, Any]],
) -> Tuple[float, float, float, float]:
    spans = _answer_span_windows(cell)
    if not spans:
        return 0.0, 0.0, 0.0, 0.0
    cited = 0
    cited_top5 = 0
    cited_route_top5 = 0
    cited_route_tail = 0
    seen: set[Tuple[str, int, int, str]] = set()
    for event in scheduled_route_events:
        data = event.get("data") or {}
        route_ranks = {
            _route_location_key(loc): rank
            for rank, loc in enumerate(
                [
                    loc
                    for loc in (
                        data.get("locations") or data.get("route_locations") or []
                    )
                    if isinstance(loc, dict)
                ],
                1,
            )
        }
        for loc in _route_unread_core_locations(cell, event):
            key = _route_location_key(loc)
            if key in seen:
                continue
            seen.add(key)
            window = _route_location_window(loc)
            if window is None:
                continue
            path, start, end = window
            overlap_ranks = [
                rank
                for span_path, span_start, span_end, rank in spans
                if span_path == path
                and _span_overlaps(start, end, span_start, span_end)
            ]
            if not overlap_ranks:
                continue
            cited += 1
            if min(overlap_ranks) <= 5:
                cited_top5 += 1
            route_rank = route_ranks.get(key)
            if route_rank is not None:
                if route_rank <= 5:
                    cited_route_top5 += 1
                else:
                    cited_route_tail += 1
    return (
        float(cited),
        float(cited_top5),
        float(cited_route_top5),
        float(cited_route_tail),
    )


def _trace_stats(cell: Dict[str, Any]) -> Dict[str, Optional[float]]:
    trace = cell.get("trace") or {}
    context = trace.get("context") or []
    events = trace.get("events") or []
    read_overlap = _read_overlap_stats(cell)
    tool_stats = _tool_event_stats(events)
    reads = [
        c for c in context if c.get("source") == "read" and c.get("state") == "read"
    ]
    rejected = [c for c in context if c.get("state") == "rejected"]
    offered = [
        c
        for c in context
        if c.get("state") == "offered" and c.get("source") != "preload"
    ]
    preload = [c for c in context if c.get("source") == "preload"]
    preload_verified = [c for c in preload if c.get("state") == "verified"]
    summarized = [c for c in context if c.get("state") == "summarized"]
    summary_meta = [c.get("metadata") or {} for c in summarized]
    compacted = any(e.get("kind") == "compaction" for e in events)
    fallback = any(e.get("kind") == "fallback" for e in events)
    lsp_route_tool_events = [
        e
        for e in events
        if e.get("kind") == "tool_call"
        and str((e.get("data") or {}).get("tool") or "") == "lsp_route"
    ]
    lsp_route_event_result_counts = [
        value
        for event in lsp_route_tool_events
        if (value := _tool_event_result_count(event.get("data") or {})) is not None
    ]
    lsp_route_tool_summaries = [
        tc for tc in cell.get("tool_calls") or [] if tc.get("skill_id") == "lsp_route"
    ]
    lsp_route_result_counts = [
        float(tc.get("n_results"))
        for tc in lsp_route_tool_summaries
        if tc.get("n_results") is not None
    ]
    graph_expansion_events = [e for e in events if e.get("kind") == "graph_expansion"]
    scheduled_anchor_audit_events = [
        e for e in events if e.get("kind") == "scheduled_anchor_audit"
    ]
    scheduled_top_anchor_audit_events = [
        e for e in events if e.get("kind") == "scheduled_top_anchor_audit"
    ]
    scheduled_ordering_audit_events = [
        e for e in events if e.get("kind") == "scheduled_answer_ordering_audit"
    ]

    def _top_anchor_mode_seen(mode: str) -> bool:
        return any(
            str((e.get("data") or {}).get("correction_mode") or "") == mode
            or str((e.get("data") or {}).get("attempted_correction_mode") or "") == mode
            for e in scheduled_top_anchor_audit_events
        )

    def _top_anchor_event_validated_deterministic(event: Dict[str, Any]) -> bool:
        data = event.get("data") or {}
        correction_mode = str(data.get("correction_mode") or "")
        attempted_mode = str(data.get("attempted_correction_mode") or "")
        if (
            correction_mode != "deterministic_answer_patch"
            and attempted_mode != "deterministic_answer_patch"
        ):
            return False
        if bool(data.get("cascade_fallback")):
            return False
        if data.get("validation_ok") is not True:
            return False
        validation_missing = data.get("validation_missing_count")
        if validation_missing is not None and float(validation_missing) > 0:
            return False
        return True

    top_anchor_validated_deterministic = bool(
        scheduled_top_anchor_audit_events
    ) and all(
        _top_anchor_event_validated_deterministic(e)
        for e in scheduled_top_anchor_audit_events
    )
    top_anchor_unvalidated = bool(scheduled_top_anchor_audit_events) and not (
        top_anchor_validated_deterministic
    )

    scheduled_context_events = [
        e for e in events if e.get("kind") == "scheduled_context"
    ]
    scheduled_final_guard_events = [
        e
        for e in scheduled_context_events
        if str((e.get("data") or {}).get("operation") or "").strip()
        == "lsp_route_finalization_guard"
    ]
    scheduled_route_events = [
        e
        for e in scheduled_context_events
        if str((e.get("data") or {}).get("operation") or "").strip() == "lsp_route"
    ]
    route_trigger_turn = _mean(
        [
            float(e.get("turn"))
            for e in scheduled_route_events
            if e.get("turn") is not None
        ]
    )

    def _route_event_mean(key: str) -> Optional[float]:
        return _mean(
            [
                float((e.get("data") or {}).get(key))
                for e in scheduled_route_events
                if (e.get("data") or {}).get(key) is not None
            ]
        )

    route_bridge_query = _mean(
        [
            1.0 if (e.get("data") or {}).get("bridge_route_query") else 0.0
            for e in scheduled_route_events
            if "bridge_route_query" in (e.get("data") or {})
        ]
    )
    scheduled_ops = [
        str((e.get("data") or {}).get("operation") or "").strip()
        for e in scheduled_context_events
    ]
    if (
        not scheduled_ops
        and cell.get("scheduled_context_triggered")
        and cell.get("scheduled_context_operation")
    ):
        scheduled_ops = [str(cell.get("scheduled_context_operation") or "").strip()]
    scheduled_ops = [op or "unknown" for op in scheduled_ops]
    (
        route_read_confirmed,
        route_read_confirmed_core,
        route_unread_core,
        route_read_confirmed_protected,
        route_unread_protected,
    ) = _route_read_memory_stats(
        cell,
        scheduled_route_events,
    )
    (
        route_unread_core_answer_cited,
        route_unread_core_answer_top5,
        route_unread_core_answer_route_top5,
        route_unread_core_answer_route_tail,
    ) = _route_unread_answer_stats(cell, scheduled_route_events)
    lsp_context = [
        c
        for c in context
        if c.get("source") in {"lsp_route", "lsp_definition", "lsp_references"}
        and c.get("state") != "rejected"
    ]
    preload_count = float(len(preload))
    preload_verified_count = float(len(preload_verified))
    answer_evidence = _summarize_answer_evidence(cell)
    replay_readiness = _summarize_replay_readiness(cell)
    pruned_tool_chars = float(
        sum(m.get("pruned_tool_result_chars") or 0 for m in summary_meta)
    )
    seed_chars = float(sum(m.get("seed_chars") or 0 for m in summary_meta))
    tail_chars = float(sum(m.get("protected_tail_chars") or 0 for m in summary_meta))
    kept_read_outputs = float(
        sum(m.get("kept_read_outputs") or 0 for m in summary_meta)
    )
    requested_tail_chars = _mean(
        [
            float(m.get("compact_tail_chars"))
            for m in summary_meta
            if m.get("compact_tail_chars") is not None
        ]
    )
    return {
        "reads": float(len(reads)),
        "tool_calls": tool_stats["tool_calls"],
        "tool_envelope_coverage": tool_stats["tool_envelope_coverage"],
        "tool_errors": tool_stats["tool_errors"],
        "tool_skipped": tool_stats["tool_skipped"],
        "tool_truncated": tool_stats["tool_truncated"],
        "tool_result_chars": tool_stats["tool_result_chars"],
        "read_repeat_offsets": read_overlap["read_repeat_offsets"],
        "read_overlap_events": read_overlap["read_overlap_events"],
        "read_overlap_lines": read_overlap["read_overlap_lines"],
        "read_overlap_rate": read_overlap["read_overlap_rate"],
        "read_overlap_pre_route_events": read_overlap["read_overlap_pre_route_events"],
        "read_overlap_pre_route_lines": read_overlap["read_overlap_pre_route_lines"],
        "read_overlap_post_route_events": read_overlap[
            "read_overlap_post_route_events"
        ],
        "read_overlap_post_route_lines": read_overlap["read_overlap_post_route_lines"],
        "read_overlap_post_route_contained_events": read_overlap[
            "read_overlap_post_route_contained_events"
        ],
        "read_overlap_post_route_expansion_events": read_overlap[
            "read_overlap_post_route_expansion_events"
        ],
        "read_overlap_post_route_shifted_events": read_overlap[
            "read_overlap_post_route_shifted_events"
        ],
        "read_overlap_low_novelty_events": read_overlap[
            "read_overlap_low_novelty_events"
        ],
        "read_overlap_post_route_low_novelty_events": read_overlap[
            "read_overlap_post_route_low_novelty_events"
        ],
        "read_overlap_post_guard_events": read_overlap[
            "read_overlap_post_guard_events"
        ],
        "read_overlap_post_guard_lines": read_overlap["read_overlap_post_guard_lines"],
        "read_overlap_unknown_phase_events": read_overlap[
            "read_overlap_unknown_phase_events"
        ],
        "read_overlap_unknown_phase_lines": read_overlap[
            "read_overlap_unknown_phase_lines"
        ],
        "rejected": float(len(rejected)),
        "offered": float(len(offered)),
        "preload": preload_count,
        "preload_verified": preload_verified_count,
        "preload_verify_rate": (
            preload_verified_count / preload_count if preload_count else None
        ),
        "answer_span_evidence_count": float(answer_evidence.get("span_count") or 0),
        "answer_trace_evidenced": float(
            answer_evidence.get("trace_evidenced_count") or 0
        ),
        "answer_trace_evidence_rate": answer_evidence.get("trace_evidenced_ratio"),
        "answer_read_confirmed": float(
            answer_evidence.get("read_confirmed_count") or 0
        ),
        "answer_read_confirmed_rate": answer_evidence.get("read_confirmed_ratio"),
        "answer_unconfirmed": float(
            len(answer_evidence.get("unconfirmed_spans") or [])
        ),
        "replay_event_replayable": (
            1.0 if replay_readiness.get("event_replayable") else 0.0
        ),
        "replay_exact_replayable": (
            1.0 if replay_readiness.get("exact_message_replayable") else 0.0
        ),
        "replay_has_message_transcript": (
            1.0 if replay_readiness.get("has_message_transcript") else 0.0
        ),
        "replay_message_count": float(replay_readiness.get("message_count") or 0),
        "replay_tool_schema_count": float(
            replay_readiness.get("tool_schema_count") or 0
        ),
        "replay_blocking_gap_count": float(
            len(replay_readiness.get("blocking_gaps") or [])
        ),
        "replay_blocking_gap_labels": [
            str(gap) for gap in replay_readiness.get("blocking_gaps") or []
        ],
        "summarized": float(len(summarized)),
        "pruned_tool_chars": pruned_tool_chars,
        "seed_chars": seed_chars,
        "tail_chars": tail_chars,
        "compact_keep_reads": kept_read_outputs,
        "compact_tail_chars": requested_tail_chars,
        "compact_prune_ratio": _ratio(
            pruned_tool_chars, pruned_tool_chars + seed_chars
        ),
        "compact_tail_share": _ratio(tail_chars, seed_chars),
        "compacted": 1.0 if compacted else 0.0,
        "fallback": 1.0 if fallback else 0.0,
        "graph_expansion": 1.0 if graph_expansion_events else 0.0,
        "graph_expansion_nodes": float(
            sum(
                (e.get("data") or {}).get("node_count") or 0
                for e in graph_expansion_events
            )
        ),
        "scheduled_context": 1.0 if scheduled_context_events else 0.0,
        "scheduled_context_nodes": float(
            sum(
                (e.get("data") or {}).get("node_count") or 0
                for e in scheduled_context_events
            )
        ),
        "scheduled_lsp_route": 1.0 if "lsp_route" in scheduled_ops else 0.0,
        "scheduled_route_trigger_turn": route_trigger_turn,
        "scheduled_route_read_calls": _route_event_mean("read_calls"),
        "scheduled_route_generic_min_reads": _route_event_mean(
            "generic_min_read_calls"
        ),
        "scheduled_route_bridge_query": route_bridge_query,
        "scheduled_route_budget_applied": (
            1.0
            if any(
                (e.get("data") or {}).get("route_budget_applied")
                for e in scheduled_route_events
            )
            else 0.0
        ),
        "scheduled_route_budget_dropped": float(
            sum(
                (e.get("data") or {}).get("route_budget_dropped") or 0
                for e in scheduled_route_events
            )
        ),
        "scheduled_route_budget_unsafe": (
            1.0
            if any(
                str((e.get("data") or {}).get("route_budget_reason") or "")
                == "unsafe_missing_core"
                for e in scheduled_route_events
            )
            else 0.0
        ),
        "scheduled_route_budget_missing_required": float(
            sum(
                (e.get("data") or {}).get("route_budget_missing_required_count") or 0
                for e in scheduled_route_events
            )
        ),
        "scheduled_route_read_confirmed": route_read_confirmed,
        "scheduled_route_read_confirmed_core": route_read_confirmed_core,
        "scheduled_route_read_confirmed_protected": route_read_confirmed_protected,
        "scheduled_route_unread_core": route_unread_core,
        "scheduled_route_unread_protected": route_unread_protected,
        "scheduled_route_unread_core_answer_cited": (route_unread_core_answer_cited),
        "scheduled_route_unread_core_answer_top5": route_unread_core_answer_top5,
        "scheduled_route_unread_core_answer_route_top5": (
            route_unread_core_answer_route_top5
        ),
        "scheduled_route_unread_core_answer_route_tail": (
            route_unread_core_answer_route_tail
        ),
        "scheduled_lsp_definition": (
            1.0 if any(op.startswith("lsp_definition") for op in scheduled_ops) else 0.0
        ),
        "scheduled_final_guard": 1.0 if scheduled_final_guard_events else 0.0,
        "scheduled_final_guard_nodes": float(
            sum(
                (e.get("data") or {}).get("node_count") or 0
                for e in scheduled_final_guard_events
            )
        ),
        "scheduled_graph_fanout": 1.0 if "graph_fanout" in scheduled_ops else 0.0,
        "scheduled_unknown_operation": (
            1.0 if "unknown" in scheduled_ops and scheduled_context_events else 0.0
        ),
        "scheduled_anchor_audit": 1.0 if scheduled_anchor_audit_events else 0.0,
        "scheduled_anchor_audit_offered": float(
            sum(
                (e.get("data") or {}).get("offered_count") or 0
                for e in scheduled_anchor_audit_events
            )
        ),
        "scheduled_anchor_audit_read": float(
            sum(
                (e.get("data") or {}).get("read_count") or 0
                for e in scheduled_anchor_audit_events
            )
        ),
        "scheduled_anchor_audit_cited": float(
            sum(
                (e.get("data") or {}).get("cited_count") or 0
                for e in scheduled_anchor_audit_events
            )
        ),
        "scheduled_top_anchor_audit": (
            1.0 if scheduled_top_anchor_audit_events else 0.0
        ),
        "scheduled_top_anchor_audit_missing": float(
            sum(
                (e.get("data") or {}).get("missing_count") or 0
                for e in scheduled_top_anchor_audit_events
            )
        ),
        "scheduled_top_anchor_audit_missing_unread": float(
            sum(
                (e.get("data") or {}).get("missing_unread_count") or 0
                for e in scheduled_top_anchor_audit_events
            )
        ),
        "scheduled_top_anchor_audit_answer_only": (
            1.0 if _top_anchor_mode_seen("answer_only") else 0.0
        ),
        "scheduled_top_anchor_audit_bounded_tool_loop": (
            1.0 if _top_anchor_mode_seen("bounded_tool_loop") else 0.0
        ),
        "scheduled_top_anchor_audit_deterministic_patch": (
            1.0 if _top_anchor_mode_seen("deterministic_answer_patch") else 0.0
        ),
        "scheduled_top_anchor_audit_validated_deterministic": (
            1.0 if top_anchor_validated_deterministic else 0.0
        ),
        "scheduled_top_anchor_audit_unvalidated": (
            1.0 if top_anchor_unvalidated else 0.0
        ),
        "scheduled_top_anchor_audit_read_only": (
            1.0 if _top_anchor_mode_seen("read_only_tool_loop") else 0.0
        ),
        "scheduled_top_anchor_audit_cascade_fallback": (
            1.0
            if any(
                bool((e.get("data") or {}).get("cascade_fallback"))
                for e in scheduled_top_anchor_audit_events
            )
            else 0.0
        ),
        "scheduled_ordering_audit": (1.0 if scheduled_ordering_audit_events else 0.0),
        "scheduled_ordering_audit_spans": float(
            sum(
                (e.get("data") or {}).get("answer_span_count") or 0
                for e in scheduled_ordering_audit_events
            )
        ),
        "lsp": float(len(lsp_context)),
        "lsp_used": 1.0 if lsp_context else 0.0,
        "lsp_route_tool_used": 1.0 if lsp_route_tool_events else 0.0,
        "lsp_route_tool_first_turn": _min(
            [
                float(e.get("turn"))
                for e in lsp_route_tool_events
                if e.get("turn") is not None
            ]
        ),
        "lsp_route_tool_results": (
            sum(lsp_route_event_result_counts)
            if lsp_route_event_result_counts
            else sum(lsp_route_result_counts) if lsp_route_result_counts else None
        ),
        "no_read": 0.0 if reads else 1.0,
        "has_trace": 1.0 if trace else 0.0,
    }


def _fold_reps(
    cells: Sequence[Dict[str, Any]]
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Fold reps by ``(query_id, arm)``.

    Accuracy and trace behavior are averaged. Cost-like values use min-of-reps,
    matching the rest of the agent-compile aggregation convention.
    """
    by: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for c in cells:
        by[(str(c.get("query_id")), str(c.get("subset_id")))].append(c)

    def _top_anchor_triggered(cell: Dict[str, Any], stats: Dict[str, Any]) -> bool:
        return bool(cell.get("scheduled_top_anchor_audit_triggered")) or bool(
            stats.get("scheduled_top_anchor_audit")
        )

    def _top_anchor_validated_deterministic(
        cell: Dict[str, Any], stats: Dict[str, Any]
    ) -> float:
        if not _top_anchor_triggered(cell, stats):
            return 0.0
        return (
            1.0
            if stats.get("scheduled_top_anchor_audit_validated_deterministic")
            else 0.0
        )

    def _top_anchor_unvalidated(cell: Dict[str, Any], stats: Dict[str, Any]) -> float:
        if not _top_anchor_triggered(cell, stats):
            return 0.0
        return 0.0 if _top_anchor_validated_deterministic(cell, stats) else 1.0

    folded: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for key, reps in by.items():
        stats = [_trace_stats(c) for c in reps]
        answer_quality = [_answer_quality_fields(c) for c in reps]
        rec_values = [_metric(c) for c in reps]
        token_values = [
            c.get("total_tokens") for c in reps if c.get("total_tokens") is not None
        ]
        turn_values = [
            c.get("total_turns") for c in reps if c.get("total_turns") is not None
        ]
        folded[key] = {
            "category": reps[0].get("category"),
            "source_config": reps[0].get("source_config"),
            "query_id": reps[0].get("query_id"),
            "arm": reps[0].get("subset_id"),
            "reps": float(len(reps)),
            "rec5": _mean(rec_values),
            "rec5_min": _min(rec_values),
            "answer_rec_all": _mean([q.get("answer_rec_all") for q in answer_quality]),
            "answer_path_alias_rec_all": _mean(
                [q.get("answer_path_alias_rec_all") for q in answer_quality]
            ),
            "files5": _mean([_metric(c, "files", 5, "recall") for c in reps]),
            "tokens": min(token_values, default=None),
            "tokens_max": max(token_values, default=None),
            "tokens_spread": (
                float(max(token_values) - min(token_values)) if token_values else None
            ),
            "turns": min(turn_values, default=None),
            "turns_max": max(turn_values, default=None),
            "turns_spread": (
                float(max(turn_values) - min(turn_values)) if turn_values else None
            ),
            "format_failed": _mean(
                [1.0 if c.get("format_failed") else 0.0 for c in reps]
            ),
            **{
                f"answer_diag_{label}": _answer_diagnosis_rate(reps, label)
                for label in ANSWER_DIAGNOSES
            },
            "answer_path_alias_normalized": _mean(
                [
                    1.0 if c.get("answer_path_alias_normalized") else 0.0
                    for c in reps
                    if c.get("answer_path_alias_normalized") is not None
                ]
            ),
            "answer_path_alias_replacements": _mean(
                [c.get("answer_path_alias_replacements") for c in reps]
            ),
            "answer_schema_salvaged": _mean(
                [
                    1.0 if c.get("answer_schema_salvaged") else 0.0
                    for c in reps
                    if c.get("answer_schema_salvaged") is not None
                ]
            ),
            "answer_schema_salvage_locations": _mean(
                [c.get("answer_schema_salvage_locations") for c in reps]
            ),
            "answer_location_order_normalized": _mean(
                [
                    1.0 if c.get("answer_location_order_normalized") else 0.0
                    for c in reps
                    if c.get("answer_location_order_normalized") is not None
                ]
            ),
            "answer_location_order_promotions": _mean(
                [c.get("answer_location_order_promotions") for c in reps]
            ),
            "contrib": _mean([c.get("preload_contribution") for c in reps]),
            "reads": _mean([s["reads"] for s in stats]),
            "tool_calls": _mean([s["tool_calls"] for s in stats]),
            "tool_envelope_coverage": _mean(
                [s["tool_envelope_coverage"] for s in stats]
            ),
            "tool_errors": _mean([s["tool_errors"] for s in stats]),
            "tool_skipped": _mean([s["tool_skipped"] for s in stats]),
            "tool_truncated": _mean([s["tool_truncated"] for s in stats]),
            "tool_result_chars": _mean([s["tool_result_chars"] for s in stats]),
            "read_repeat_offsets": _mean([s["read_repeat_offsets"] for s in stats]),
            "read_overlap_events": _mean([s["read_overlap_events"] for s in stats]),
            "read_overlap_lines": _mean([s["read_overlap_lines"] for s in stats]),
            "read_overlap_rate": _mean([s["read_overlap_rate"] for s in stats]),
            "read_overlap_pre_route_events": _mean(
                [s["read_overlap_pre_route_events"] for s in stats]
            ),
            "read_overlap_pre_route_lines": _mean(
                [s["read_overlap_pre_route_lines"] for s in stats]
            ),
            "read_overlap_post_route_events": _mean(
                [s["read_overlap_post_route_events"] for s in stats]
            ),
            "read_overlap_post_route_lines": _mean(
                [s["read_overlap_post_route_lines"] for s in stats]
            ),
            "read_overlap_post_route_contained_events": _mean(
                [s["read_overlap_post_route_contained_events"] for s in stats]
            ),
            "read_overlap_post_route_expansion_events": _mean(
                [s["read_overlap_post_route_expansion_events"] for s in stats]
            ),
            "read_overlap_post_route_shifted_events": _mean(
                [s["read_overlap_post_route_shifted_events"] for s in stats]
            ),
            "read_overlap_low_novelty_events": _mean(
                [s["read_overlap_low_novelty_events"] for s in stats]
            ),
            "read_overlap_post_route_low_novelty_events": _mean(
                [s["read_overlap_post_route_low_novelty_events"] for s in stats]
            ),
            "read_overlap_post_guard_events": _mean(
                [s["read_overlap_post_guard_events"] for s in stats]
            ),
            "read_overlap_post_guard_lines": _mean(
                [s["read_overlap_post_guard_lines"] for s in stats]
            ),
            "read_overlap_unknown_phase_events": _mean(
                [s["read_overlap_unknown_phase_events"] for s in stats]
            ),
            "read_overlap_unknown_phase_lines": _mean(
                [s["read_overlap_unknown_phase_lines"] for s in stats]
            ),
            "rejected": _mean([s["rejected"] for s in stats]),
            "offered": _mean([s["offered"] for s in stats]),
            "preload": _mean([s["preload"] for s in stats]),
            "preload_verified": _mean([s["preload_verified"] for s in stats]),
            "preload_verify_rate": _mean([s["preload_verify_rate"] for s in stats]),
            "answer_span_evidence_count": _mean(
                [s["answer_span_evidence_count"] for s in stats]
            ),
            "answer_span_evidence_count_min": _min(
                [s["answer_span_evidence_count"] for s in stats]
            ),
            "answer_trace_evidenced": _mean(
                [s["answer_trace_evidenced"] for s in stats]
            ),
            "answer_trace_evidence_rate": _mean(
                [s["answer_trace_evidence_rate"] for s in stats]
            ),
            "answer_trace_evidence_rate_min": _min(
                [s["answer_trace_evidence_rate"] for s in stats]
            ),
            "answer_read_confirmed": _mean([s["answer_read_confirmed"] for s in stats]),
            "answer_read_confirmed_rate": _mean(
                [s["answer_read_confirmed_rate"] for s in stats]
            ),
            "answer_read_confirmed_rate_min": _min(
                [s["answer_read_confirmed_rate"] for s in stats]
            ),
            "answer_unconfirmed": _mean([s["answer_unconfirmed"] for s in stats]),
            "answer_unconfirmed_max": _max([s["answer_unconfirmed"] for s in stats]),
            "replay_event_replayable": _mean(
                [s["replay_event_replayable"] for s in stats]
            ),
            "replay_event_replayable_min": _min(
                [s["replay_event_replayable"] for s in stats]
            ),
            "replay_exact_replayable": _mean(
                [s["replay_exact_replayable"] for s in stats]
            ),
            "replay_exact_replayable_min": _min(
                [s["replay_exact_replayable"] for s in stats]
            ),
            "replay_has_message_transcript": _mean(
                [s["replay_has_message_transcript"] for s in stats]
            ),
            "replay_message_count": _mean([s["replay_message_count"] for s in stats]),
            "replay_tool_schema_count": _mean(
                [s["replay_tool_schema_count"] for s in stats]
            ),
            "replay_blocking_gap_count": _mean(
                [s["replay_blocking_gap_count"] for s in stats]
            ),
            "replay_blocking_gap_count_max": _max(
                [s["replay_blocking_gap_count"] for s in stats]
            ),
            "replay_blocking_gap_labels": sorted(
                {
                    str(gap)
                    for s in stats
                    for gap in s.get("replay_blocking_gap_labels", [])
                }
            ),
            "summarized": _mean([s["summarized"] for s in stats]),
            "pruned_tool_chars": _mean([s["pruned_tool_chars"] for s in stats]),
            "seed_chars": _mean([s["seed_chars"] for s in stats]),
            "tail_chars": _mean([s["tail_chars"] for s in stats]),
            "compact_keep_reads": _mean([s["compact_keep_reads"] for s in stats]),
            "compact_tail_chars": _mean([s["compact_tail_chars"] for s in stats]),
            "compact_prune_ratio": _mean([s["compact_prune_ratio"] for s in stats]),
            "compact_tail_share": _mean([s["compact_tail_share"] for s in stats]),
            "compacted": _mean([s["compacted"] for s in stats]),
            "fallback": _mean([s["fallback"] for s in stats]),
            "graph_expansion": _mean(
                [
                    (
                        1.0
                        if c.get("graph_expansion_triggered")
                        else s["graph_expansion"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "graph_expansion_nodes": _mean(
                [
                    (
                        float(c.get("graph_expansion_nodes") or 0)
                        if c.get("graph_expansion_triggered")
                        else s["graph_expansion_nodes"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "lsp": _mean([s["lsp"] for s in stats]),
            "lsp_used": _mean([s["lsp_used"] for s in stats]),
            "lsp_route_tool_used": _mean([s["lsp_route_tool_used"] for s in stats]),
            "lsp_route_tool_first_turn": _mean(
                [s["lsp_route_tool_first_turn"] for s in stats]
            ),
            "lsp_route_tool_results": _mean(
                [s["lsp_route_tool_results"] for s in stats]
            ),
            "scheduled_context": _mean(
                [
                    (
                        1.0
                        if c.get("scheduled_context_triggered")
                        else s["scheduled_context"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_context_nodes": _mean(
                [
                    (
                        float(c.get("scheduled_context_nodes") or 0)
                        if c.get("scheduled_context_triggered")
                        else s["scheduled_context_nodes"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_lsp_route": _mean([s["scheduled_lsp_route"] for s in stats]),
            "scheduled_route_trigger_turn": _mean(
                [s["scheduled_route_trigger_turn"] for s in stats]
            ),
            "scheduled_route_read_calls": _mean(
                [s["scheduled_route_read_calls"] for s in stats]
            ),
            "scheduled_route_generic_min_reads": _mean(
                [s["scheduled_route_generic_min_reads"] for s in stats]
            ),
            "scheduled_route_bridge_query": _mean(
                [s["scheduled_route_bridge_query"] for s in stats]
            ),
            "scheduled_route_fanout_hold_count": _mean(
                [c.get("scheduled_route_fanout_hold_count") for c in reps]
            ),
            "scheduled_route_fanout_hold_first_turn": _mean(
                [c.get("scheduled_route_fanout_hold_first_turn") for c in reps]
            ),
            "scheduled_route_fanout_hold_read_calls": _mean(
                [c.get("scheduled_route_fanout_hold_read_calls") for c in reps]
            ),
            "scheduled_route_fanout_hold_search_calls": _mean(
                [c.get("scheduled_route_fanout_hold_search_calls") for c in reps]
            ),
            "scheduled_route_fanout_hold_generic_min_reads": _mean(
                [c.get("scheduled_route_fanout_hold_generic_min_reads") for c in reps]
            ),
            "scheduled_route_budget_applied": _mean(
                [s["scheduled_route_budget_applied"] for s in stats]
            ),
            "scheduled_route_budget_dropped": _mean(
                [s["scheduled_route_budget_dropped"] for s in stats]
            ),
            "scheduled_route_budget_unsafe": _mean(
                [s["scheduled_route_budget_unsafe"] for s in stats]
            ),
            "scheduled_route_budget_missing_required": _mean(
                [s["scheduled_route_budget_missing_required"] for s in stats]
            ),
            "scheduled_route_read_confirmed": _mean(
                [s["scheduled_route_read_confirmed"] for s in stats]
            ),
            "scheduled_route_read_confirmed_core": _mean(
                [s["scheduled_route_read_confirmed_core"] for s in stats]
            ),
            "scheduled_route_read_confirmed_protected": _mean(
                [s["scheduled_route_read_confirmed_protected"] for s in stats]
            ),
            "scheduled_route_unread_core": _mean(
                [s["scheduled_route_unread_core"] for s in stats]
            ),
            "scheduled_route_unread_protected": _mean(
                [s["scheduled_route_unread_protected"] for s in stats]
            ),
            "scheduled_route_unread_core_answer_cited": _mean(
                [s["scheduled_route_unread_core_answer_cited"] for s in stats]
            ),
            "scheduled_route_unread_core_answer_top5": _mean(
                [s["scheduled_route_unread_core_answer_top5"] for s in stats]
            ),
            "scheduled_route_unread_core_answer_route_top5": _mean(
                [s["scheduled_route_unread_core_answer_route_top5"] for s in stats]
            ),
            "scheduled_route_unread_core_answer_route_tail": _mean(
                [s["scheduled_route_unread_core_answer_route_tail"] for s in stats]
            ),
            "scheduled_lsp_definition": _mean(
                [s["scheduled_lsp_definition"] for s in stats]
            ),
            "scheduled_final_guard": _mean([s["scheduled_final_guard"] for s in stats]),
            "scheduled_final_guard_nodes": _mean(
                [s["scheduled_final_guard_nodes"] for s in stats]
            ),
            "scheduled_graph_fanout": _mean(
                [s["scheduled_graph_fanout"] for s in stats]
            ),
            "scheduled_unknown_operation": _mean(
                [s["scheduled_unknown_operation"] for s in stats]
            ),
            "scheduled_context_skipped": _mean(
                [1.0 if c.get("scheduled_context_skipped") else 0.0 for c in reps]
            ),
            "scheduled_anchor_audit": _mean(
                [
                    (
                        1.0
                        if c.get("scheduled_anchor_audit_triggered")
                        else s["scheduled_anchor_audit"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_anchor_audit_offered": _mean(
                [
                    (
                        float(c.get("scheduled_anchor_audit_offered") or 0)
                        if c.get("scheduled_anchor_audit_offered") is not None
                        else s["scheduled_anchor_audit_offered"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_anchor_audit_read": _mean(
                [
                    (
                        float(c.get("scheduled_anchor_audit_read") or 0)
                        if c.get("scheduled_anchor_audit_read") is not None
                        else s["scheduled_anchor_audit_read"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_anchor_audit_cited": _mean(
                [
                    (
                        float(c.get("scheduled_anchor_audit_cited") or 0)
                        if c.get("scheduled_anchor_audit_cited") is not None
                        else s["scheduled_anchor_audit_cited"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_top_anchor_audit": _mean(
                [
                    (
                        1.0
                        if c.get("scheduled_top_anchor_audit_triggered")
                        else s["scheduled_top_anchor_audit"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_top_anchor_audit_missing": _mean(
                [
                    (
                        float(c.get("scheduled_top_anchor_audit_missing") or 0)
                        if c.get("scheduled_top_anchor_audit_missing") is not None
                        else s["scheduled_top_anchor_audit_missing"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_top_anchor_audit_missing_unread": _mean(
                [
                    (
                        float(c.get("scheduled_top_anchor_audit_missing_unread") or 0)
                        if c.get("scheduled_top_anchor_audit_missing_unread")
                        is not None
                        else s["scheduled_top_anchor_audit_missing_unread"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_top_anchor_audit_answer_only": _mean(
                [s["scheduled_top_anchor_audit_answer_only"] for s in stats]
            ),
            "scheduled_top_anchor_audit_bounded_tool_loop": _mean(
                [s["scheduled_top_anchor_audit_bounded_tool_loop"] for s in stats]
            ),
            "scheduled_top_anchor_audit_deterministic_patch": _mean(
                [s["scheduled_top_anchor_audit_deterministic_patch"] for s in stats]
            ),
            "scheduled_top_anchor_audit_validated_deterministic": _mean(
                [
                    _top_anchor_validated_deterministic(c, s)
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_top_anchor_audit_unvalidated": _mean(
                [
                    _top_anchor_unvalidated(c, s)
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_top_anchor_audit_read_only": _mean(
                [s["scheduled_top_anchor_audit_read_only"] for s in stats]
            ),
            "scheduled_top_anchor_audit_cascade_fallback": _mean(
                [s["scheduled_top_anchor_audit_cascade_fallback"] for s in stats]
            ),
            "scheduled_ordering_audit": _mean(
                [
                    (
                        1.0
                        if c.get("scheduled_ordering_audit_triggered")
                        else s["scheduled_ordering_audit"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "scheduled_ordering_audit_spans": _mean(
                [
                    (
                        float(c.get("scheduled_ordering_audit_span_count") or 0)
                        if c.get("scheduled_ordering_audit_span_count") is not None
                        else s["scheduled_ordering_audit_spans"]
                    )
                    for c, s in zip(reps, stats, strict=False)
                ]
            ),
            "no_read": _mean([s["no_read"] for s in stats]),
            "has_trace": _mean([s["has_trace"] for s in stats]),
        }
    return folded


def aggregate(cells: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    folded = _fold_reps(cells)
    by_arm: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_cat_arm: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for (_qid, _arm), row in folded.items():
        arm = str(row["arm"])
        cat = str(row.get("category"))
        by_arm[arm].append(row)
        by_cat_arm[(cat, arm)].append(row)

    def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "n": len(rows),
            "reps": _mean([r.get("reps") for r in rows]),
            "reps_min": _min([r.get("reps") for r in rows]),
            "rec5": _mean([r.get("rec5") for r in rows]),
            "rec5_min": _min([r.get("rec5_min") for r in rows]),
            "answer_rec_all": _mean([r.get("answer_rec_all") for r in rows]),
            "answer_path_alias_rec_all": _mean(
                [r.get("answer_path_alias_rec_all") for r in rows]
            ),
            "files5": _mean([r.get("files5") for r in rows]),
            "tokens": _mean([r.get("tokens") for r in rows]),
            "tokens_max": _mean([r.get("tokens_max") for r in rows]),
            "tokens_spread": _mean([r.get("tokens_spread") for r in rows]),
            "turns": _mean([r.get("turns") for r in rows]),
            "turns_max": _mean([r.get("turns_max") for r in rows]),
            "turns_spread": _mean([r.get("turns_spread") for r in rows]),
            "format_failed": _mean([r.get("format_failed") for r in rows]),
            **{
                f"answer_diag_{label}": _mean(
                    [r.get(f"answer_diag_{label}") for r in rows]
                )
                for label in ANSWER_DIAGNOSES
            },
            "answer_path_alias_normalized": _mean(
                [r.get("answer_path_alias_normalized") for r in rows]
            ),
            "answer_path_alias_replacements": _mean(
                [r.get("answer_path_alias_replacements") for r in rows]
            ),
            "answer_schema_salvaged": _mean(
                [r.get("answer_schema_salvaged") for r in rows]
            ),
            "answer_schema_salvage_locations": _mean(
                [r.get("answer_schema_salvage_locations") for r in rows]
            ),
            "answer_location_order_normalized": _mean(
                [r.get("answer_location_order_normalized") for r in rows]
            ),
            "answer_location_order_promotions": _mean(
                [r.get("answer_location_order_promotions") for r in rows]
            ),
            "contrib": _mean([r.get("contrib") for r in rows]),
            "reads": _mean([r.get("reads") for r in rows]),
            "tool_calls": _mean([r.get("tool_calls") for r in rows]),
            "tool_envelope_coverage": _mean(
                [r.get("tool_envelope_coverage") for r in rows]
            ),
            "tool_errors": _mean([r.get("tool_errors") for r in rows]),
            "tool_skipped": _mean([r.get("tool_skipped") for r in rows]),
            "tool_truncated": _mean([r.get("tool_truncated") for r in rows]),
            "tool_result_chars": _mean([r.get("tool_result_chars") for r in rows]),
            "read_repeat_offsets": _mean([r.get("read_repeat_offsets") for r in rows]),
            "read_overlap_events": _mean([r.get("read_overlap_events") for r in rows]),
            "read_overlap_lines": _mean([r.get("read_overlap_lines") for r in rows]),
            "read_overlap_rate": _mean([r.get("read_overlap_rate") for r in rows]),
            "read_overlap_pre_route_events": _mean(
                [r.get("read_overlap_pre_route_events") for r in rows]
            ),
            "read_overlap_pre_route_lines": _mean(
                [r.get("read_overlap_pre_route_lines") for r in rows]
            ),
            "read_overlap_post_route_events": _mean(
                [r.get("read_overlap_post_route_events") for r in rows]
            ),
            "read_overlap_post_route_lines": _mean(
                [r.get("read_overlap_post_route_lines") for r in rows]
            ),
            "read_overlap_post_route_contained_events": _mean(
                [r.get("read_overlap_post_route_contained_events") for r in rows]
            ),
            "read_overlap_post_route_expansion_events": _mean(
                [r.get("read_overlap_post_route_expansion_events") for r in rows]
            ),
            "read_overlap_post_route_shifted_events": _mean(
                [r.get("read_overlap_post_route_shifted_events") for r in rows]
            ),
            "read_overlap_low_novelty_events": _mean(
                [r.get("read_overlap_low_novelty_events") for r in rows]
            ),
            "read_overlap_post_route_low_novelty_events": _mean(
                [r.get("read_overlap_post_route_low_novelty_events") for r in rows]
            ),
            "read_overlap_post_guard_events": _mean(
                [r.get("read_overlap_post_guard_events") for r in rows]
            ),
            "read_overlap_post_guard_lines": _mean(
                [r.get("read_overlap_post_guard_lines") for r in rows]
            ),
            "read_overlap_unknown_phase_events": _mean(
                [r.get("read_overlap_unknown_phase_events") for r in rows]
            ),
            "read_overlap_unknown_phase_lines": _mean(
                [r.get("read_overlap_unknown_phase_lines") for r in rows]
            ),
            "rejected": _mean([r.get("rejected") for r in rows]),
            "offered": _mean([r.get("offered") for r in rows]),
            "preload": _mean([r.get("preload") for r in rows]),
            "preload_verified": _mean([r.get("preload_verified") for r in rows]),
            "preload_verify_rate": _mean([r.get("preload_verify_rate") for r in rows]),
            "answer_span_evidence_count": _mean(
                [r.get("answer_span_evidence_count") for r in rows]
            ),
            "answer_span_evidence_count_min": _min(
                [r.get("answer_span_evidence_count_min") for r in rows]
            ),
            "answer_trace_evidenced": _mean(
                [r.get("answer_trace_evidenced") for r in rows]
            ),
            "answer_trace_evidence_rate": _mean(
                [r.get("answer_trace_evidence_rate") for r in rows]
            ),
            "answer_trace_evidence_rate_min": _min(
                [r.get("answer_trace_evidence_rate_min") for r in rows]
            ),
            "answer_read_confirmed": _mean(
                [r.get("answer_read_confirmed") for r in rows]
            ),
            "answer_read_confirmed_rate": _mean(
                [r.get("answer_read_confirmed_rate") for r in rows]
            ),
            "answer_read_confirmed_rate_min": _min(
                [r.get("answer_read_confirmed_rate_min") for r in rows]
            ),
            "answer_unconfirmed": _mean([r.get("answer_unconfirmed") for r in rows]),
            "answer_unconfirmed_max": _max(
                [r.get("answer_unconfirmed_max") for r in rows]
            ),
            "replay_event_replayable": _mean(
                [r.get("replay_event_replayable") for r in rows]
            ),
            "replay_event_replayable_min": _min(
                [r.get("replay_event_replayable_min") for r in rows]
            ),
            "replay_exact_replayable": _mean(
                [r.get("replay_exact_replayable") for r in rows]
            ),
            "replay_exact_replayable_min": _min(
                [r.get("replay_exact_replayable_min") for r in rows]
            ),
            "replay_has_message_transcript": _mean(
                [r.get("replay_has_message_transcript") for r in rows]
            ),
            "replay_message_count": _mean(
                [r.get("replay_message_count") for r in rows]
            ),
            "replay_tool_schema_count": _mean(
                [r.get("replay_tool_schema_count") for r in rows]
            ),
            "replay_blocking_gap_count": _mean(
                [r.get("replay_blocking_gap_count") for r in rows]
            ),
            "replay_blocking_gap_count_max": _max(
                [r.get("replay_blocking_gap_count_max") for r in rows]
            ),
            "replay_blocking_gap_labels": sorted(
                {
                    str(gap)
                    for r in rows
                    for gap in (r.get("replay_blocking_gap_labels") or [])
                }
            ),
            "summarized": _mean([r.get("summarized") for r in rows]),
            "pruned_tool_chars": _mean([r.get("pruned_tool_chars") for r in rows]),
            "seed_chars": _mean([r.get("seed_chars") for r in rows]),
            "tail_chars": _mean([r.get("tail_chars") for r in rows]),
            "compact_keep_reads": _mean([r.get("compact_keep_reads") for r in rows]),
            "compact_tail_chars": _mean([r.get("compact_tail_chars") for r in rows]),
            "compact_prune_ratio": _mean([r.get("compact_prune_ratio") for r in rows]),
            "compact_tail_share": _mean([r.get("compact_tail_share") for r in rows]),
            "compacted": _mean([r.get("compacted") for r in rows]),
            "fallback": _mean([r.get("fallback") for r in rows]),
            "graph_expansion": _mean([r.get("graph_expansion") for r in rows]),
            "graph_expansion_nodes": _mean(
                [r.get("graph_expansion_nodes") for r in rows]
            ),
            "lsp": _mean([r.get("lsp") for r in rows]),
            "lsp_used": _mean([r.get("lsp_used") for r in rows]),
            "lsp_route_tool_used": _mean([r.get("lsp_route_tool_used") for r in rows]),
            "lsp_route_tool_first_turn": _mean(
                [r.get("lsp_route_tool_first_turn") for r in rows]
            ),
            "lsp_route_tool_results": _mean(
                [r.get("lsp_route_tool_results") for r in rows]
            ),
            "scheduled_context": _mean([r.get("scheduled_context") for r in rows]),
            "scheduled_context_nodes": _mean(
                [r.get("scheduled_context_nodes") for r in rows]
            ),
            "scheduled_lsp_route": _mean([r.get("scheduled_lsp_route") for r in rows]),
            "scheduled_route_trigger_turn": _mean(
                [r.get("scheduled_route_trigger_turn") for r in rows]
            ),
            "scheduled_route_read_calls": _mean(
                [r.get("scheduled_route_read_calls") for r in rows]
            ),
            "scheduled_route_generic_min_reads": _mean(
                [r.get("scheduled_route_generic_min_reads") for r in rows]
            ),
            "scheduled_route_bridge_query": _mean(
                [r.get("scheduled_route_bridge_query") for r in rows]
            ),
            "scheduled_route_fanout_hold_count": _mean(
                [r.get("scheduled_route_fanout_hold_count") for r in rows]
            ),
            "scheduled_route_fanout_hold_first_turn": _mean(
                [r.get("scheduled_route_fanout_hold_first_turn") for r in rows]
            ),
            "scheduled_route_fanout_hold_read_calls": _mean(
                [r.get("scheduled_route_fanout_hold_read_calls") for r in rows]
            ),
            "scheduled_route_fanout_hold_search_calls": _mean(
                [r.get("scheduled_route_fanout_hold_search_calls") for r in rows]
            ),
            "scheduled_route_fanout_hold_generic_min_reads": _mean(
                [r.get("scheduled_route_fanout_hold_generic_min_reads") for r in rows]
            ),
            "scheduled_route_budget_applied": _mean(
                [r.get("scheduled_route_budget_applied") for r in rows]
            ),
            "scheduled_route_budget_dropped": _mean(
                [r.get("scheduled_route_budget_dropped") for r in rows]
            ),
            "scheduled_route_budget_unsafe": _mean(
                [r.get("scheduled_route_budget_unsafe") for r in rows]
            ),
            "scheduled_route_budget_missing_required": _mean(
                [r.get("scheduled_route_budget_missing_required") for r in rows]
            ),
            "scheduled_route_read_confirmed": _mean(
                [r.get("scheduled_route_read_confirmed") for r in rows]
            ),
            "scheduled_route_read_confirmed_core": _mean(
                [r.get("scheduled_route_read_confirmed_core") for r in rows]
            ),
            "scheduled_route_read_confirmed_protected": _mean(
                [r.get("scheduled_route_read_confirmed_protected") for r in rows]
            ),
            "scheduled_route_unread_core": _mean(
                [r.get("scheduled_route_unread_core") for r in rows]
            ),
            "scheduled_route_unread_protected": _mean(
                [r.get("scheduled_route_unread_protected") for r in rows]
            ),
            "scheduled_route_unread_core_answer_cited": _mean(
                [r.get("scheduled_route_unread_core_answer_cited") for r in rows]
            ),
            "scheduled_route_unread_core_answer_top5": _mean(
                [r.get("scheduled_route_unread_core_answer_top5") for r in rows]
            ),
            "scheduled_route_unread_core_answer_route_top5": _mean(
                [r.get("scheduled_route_unread_core_answer_route_top5") for r in rows]
            ),
            "scheduled_route_unread_core_answer_route_tail": _mean(
                [r.get("scheduled_route_unread_core_answer_route_tail") for r in rows]
            ),
            "scheduled_lsp_definition": _mean(
                [r.get("scheduled_lsp_definition") for r in rows]
            ),
            "scheduled_final_guard": _mean(
                [r.get("scheduled_final_guard") for r in rows]
            ),
            "scheduled_final_guard_nodes": _mean(
                [r.get("scheduled_final_guard_nodes") for r in rows]
            ),
            "scheduled_graph_fanout": _mean(
                [r.get("scheduled_graph_fanout") for r in rows]
            ),
            "scheduled_unknown_operation": _mean(
                [r.get("scheduled_unknown_operation") for r in rows]
            ),
            "scheduled_context_skipped": _mean(
                [r.get("scheduled_context_skipped") for r in rows]
            ),
            "scheduled_anchor_audit": _mean(
                [r.get("scheduled_anchor_audit") for r in rows]
            ),
            "scheduled_anchor_audit_offered": _mean(
                [r.get("scheduled_anchor_audit_offered") for r in rows]
            ),
            "scheduled_anchor_audit_read": _mean(
                [r.get("scheduled_anchor_audit_read") for r in rows]
            ),
            "scheduled_anchor_audit_cited": _mean(
                [r.get("scheduled_anchor_audit_cited") for r in rows]
            ),
            "scheduled_top_anchor_audit": _mean(
                [r.get("scheduled_top_anchor_audit") for r in rows]
            ),
            "scheduled_top_anchor_audit_missing": _mean(
                [r.get("scheduled_top_anchor_audit_missing") for r in rows]
            ),
            "scheduled_top_anchor_audit_missing_unread": _mean(
                [r.get("scheduled_top_anchor_audit_missing_unread") for r in rows]
            ),
            "scheduled_top_anchor_audit_answer_only": _mean(
                [r.get("scheduled_top_anchor_audit_answer_only") for r in rows]
            ),
            "scheduled_top_anchor_audit_bounded_tool_loop": _mean(
                [r.get("scheduled_top_anchor_audit_bounded_tool_loop") for r in rows]
            ),
            "scheduled_top_anchor_audit_deterministic_patch": _mean(
                [r.get("scheduled_top_anchor_audit_deterministic_patch") for r in rows]
            ),
            "scheduled_top_anchor_audit_validated_deterministic": _mean(
                [
                    r.get("scheduled_top_anchor_audit_validated_deterministic")
                    for r in rows
                ]
            ),
            "scheduled_top_anchor_audit_unvalidated": _mean(
                [r.get("scheduled_top_anchor_audit_unvalidated") for r in rows]
            ),
            "scheduled_top_anchor_audit_read_only": _mean(
                [r.get("scheduled_top_anchor_audit_read_only") for r in rows]
            ),
            "scheduled_top_anchor_audit_cascade_fallback": _mean(
                [r.get("scheduled_top_anchor_audit_cascade_fallback") for r in rows]
            ),
            "scheduled_ordering_audit": _mean(
                [r.get("scheduled_ordering_audit") for r in rows]
            ),
            "scheduled_ordering_audit_spans": _mean(
                [r.get("scheduled_ordering_audit_spans") for r in rows]
            ),
            "no_read": _mean([r.get("no_read") for r in rows]),
            "has_trace": _mean([r.get("has_trace") for r in rows]),
        }

    paired: Dict[Tuple[str, str], Dict[str, Any]] = {}
    arms = sorted(by_arm)
    cats = sorted({c for (c, _a) in by_cat_arm})
    for cat in cats + ["ALL"]:
        for arm in arms:
            if arm == BASELINE:
                continue
            drec: List[float] = []
            drec_all: List[float] = []
            dalias_all: List[float] = []
            dtok: List[float] = []
            dturns: List[float] = []
            common = []
            for (qid, _), base in folded.items():
                if base.get("arm") != BASELINE:
                    continue
                if cat != "ALL" and base.get("category") != cat:
                    continue
                other = folded.get((qid, arm))
                if not other:
                    continue
                common.append(qid)
                if base.get("rec5") is not None and other.get("rec5") is not None:
                    drec.append(other["rec5"] - base["rec5"])
                if (
                    base.get("answer_rec_all") is not None
                    and other.get("answer_rec_all") is not None
                ):
                    drec_all.append(other["answer_rec_all"] - base["answer_rec_all"])
                if (
                    base.get("answer_path_alias_rec_all") is not None
                    and other.get("answer_path_alias_rec_all") is not None
                ):
                    dalias_all.append(
                        other["answer_path_alias_rec_all"]
                        - base["answer_path_alias_rec_all"]
                    )
                if base.get("tokens") and other.get("tokens") is not None:
                    dtok.append(other["tokens"] - base["tokens"])
                if base.get("turns") is not None and other.get("turns") is not None:
                    dturns.append(other["turns"] - base["turns"])
            paired[(cat, arm)] = {
                "n": len(common),
                "delta_rec5": _mean(drec),
                "delta_answer_rec_all": _mean(drec_all),
                "delta_answer_path_alias_rec_all": _mean(dalias_all),
                "delta_tokens": _mean(dtok),
                "delta_turns": _mean(dturns),
            }

    by_arm_summary = {arm: summarize(rows) for arm, rows in by_arm.items()}
    paired_summary = {f"{cat}/{arm}": v for (cat, arm), v in paired.items()}
    compact_policy = {
        arm: _compact_policy_hint(row, paired_summary.get(f"ALL/{arm}", {}))
        for arm, row in by_arm_summary.items()
    }
    compact_policy_plan = {
        arm: _compact_policy_plan(
            row,
            paired_summary.get(f"ALL/{arm}", {}),
            compact_policy.get(arm, "n/a"),
        )
        for arm, row in by_arm_summary.items()
    }
    answer_diagnosis_plan = {
        arm: _answer_diagnosis_plan(row, paired_summary.get(f"ALL/{arm}", {}))
        for arm, row in by_arm_summary.items()
    }
    tool_envelope_plan = {
        arm: _tool_envelope_plan(row) for arm, row in by_arm_summary.items()
    }
    route_lifecycle_gate = _route_lifecycle_gate(by_arm_summary, paired_summary)

    return {
        "by_arm": by_arm_summary,
        "by_cat_arm": {
            f"{cat}/{arm}": summarize(rows) for (cat, arm), rows in by_cat_arm.items()
        },
        "paired": paired_summary,
        "compact_policy": compact_policy,
        "compact_policy_plan": compact_policy_plan,
        "answer_diagnosis_plan": answer_diagnosis_plan,
        "tool_envelope_plan": tool_envelope_plan,
        "route_lifecycle_gate": route_lifecycle_gate,
    }


def _verdict(delta_rec: Optional[float], delta_tokens: Optional[float]) -> str:
    if delta_rec is None or delta_tokens is None:
        return "n/a"
    if delta_rec < -EPS:
        return "REGRESS"
    if delta_tokens < 0:
        return "SAVE"
    return "costlier"


def _compact_policy_hint(row: Dict[str, Any], paired: Dict[str, Any]) -> str:
    if (row.get("compacted") or 0.0) < 0.5:
        return "n/a"
    delta_rec = paired.get("delta_rec5")
    delta_tokens = paired.get("delta_tokens")
    delta_turns = paired.get("delta_turns")
    if delta_rec is not None and delta_rec < -EPS:
        return "rec_regress"
    if (
        delta_tokens is not None
        and delta_tokens > 0
        and (delta_turns is None or delta_turns >= 0)
    ):
        return "costlier"
    if (row.get("read_repeat_offsets") or 0.0) >= 0.5 and (
        row.get("compact_keep_reads") or 0.0
    ) < 1.0:
        return "try_keep1"
    tail_share = row.get("compact_tail_share")
    if tail_share is not None and tail_share > 0.35:
        return "trim_tail"
    prune_ratio = row.get("compact_prune_ratio")
    if prune_ratio is not None and prune_ratio < 0.5:
        return "weak_prune"
    if (delta_tokens is not None and delta_tokens < 0) or (
        delta_turns is not None and delta_turns < 0
    ):
        return "hold"
    return "watch"


def _compact_policy_plan(
    row: Dict[str, Any], paired: Dict[str, Any], policy: str
) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    action = "collect_more_data"
    reason = policy
    if policy == "hold":
        action = "keep_current_compact_policy"
        reason = "compact saved tokens or turns without a recall regression"
    elif policy == "try_keep1":
        action = "set_compact_keep_reads"
        patch["compact_keep_reads"] = 1
        reason = "repeated read starts under keep0 indicate a re-read tax"
    elif policy == "trim_tail":
        action = "set_compact_tail_chars"
        current = row.get("compact_tail_chars") or row.get("tail_chars") or 800
        patch["compact_tail_chars"] = max(200, int(float(current) * 0.5))
        reason = "protected tail dominates the compact seed"
    elif policy == "weak_prune":
        action = "inspect_compaction_seed"
        reason = "compaction did not prune enough stale tool output"
    elif policy == "rec_regress":
        action = "reject_or_restore_previous_policy"
        reason = "answer recall regressed beyond the M2 tolerance"
    elif policy == "costlier":
        action = "reject_or_reduce_seed_cost"
        reason = "compact arm cost more without a turn reduction"
    elif policy == "n/a":
        action = "not_a_compact_arm"
        reason = "arm did not compact in enough cells"
    return {
        "policy": policy,
        "action": action,
        "preload_patch": patch,
        "reason": reason,
        "evidence": {
            "delta_rec5": paired.get("delta_rec5"),
            "delta_tokens": paired.get("delta_tokens"),
            "delta_turns": paired.get("delta_turns"),
            "read_repeat_offsets": row.get("read_repeat_offsets"),
            "compact_keep_reads": row.get("compact_keep_reads"),
            "compact_tail_chars": row.get("compact_tail_chars"),
            "compact_tail_share": row.get("compact_tail_share"),
            "compact_prune_ratio": row.get("compact_prune_ratio"),
        },
    }


def _compact_patch_text(plan: Dict[str, Any]) -> str:
    patch = plan.get("preload_patch") or {}
    if not patch:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(patch.items()))


def _tool_envelope_plan(row: Dict[str, Any]) -> Dict[str, Any]:
    tool_calls = row.get("tool_calls")
    coverage = row.get("tool_envelope_coverage")
    errors = row.get("tool_errors") or 0.0
    skipped = row.get("tool_skipped") or 0.0
    truncated = row.get("tool_truncated") or 0.0
    evidence = {
        "tool_calls": tool_calls,
        "tool_envelope_coverage": coverage,
        "tool_errors": row.get("tool_errors"),
        "tool_skipped": row.get("tool_skipped"),
        "tool_truncated": row.get("tool_truncated"),
        "tool_result_chars": row.get("tool_result_chars"),
    }
    if not tool_calls:
        return {
            "status": "missing",
            "action": "collect_schema_v2_tool_trace",
            "reason": "No trace tool calls were present for this arm.",
            "evidence": evidence,
        }
    if coverage is None or coverage < 1.0:
        return {
            "status": "incomplete",
            "action": "complete_tool_envelope_coverage",
            "reason": "Some tool-call trace events lack typed result envelopes.",
            "evidence": evidence,
        }
    if errors > 0:
        return {
            "status": "fail",
            "action": "inspect_typed_tool_errors",
            "reason": "Typed envelopes report tool errors in this arm.",
            "evidence": evidence,
        }
    if skipped > 0:
        return {
            "status": "watch",
            "action": "inspect_tool_skips",
            "reason": "Typed envelopes report skipped tool calls.",
            "evidence": evidence,
        }
    if truncated > 0:
        return {
            "status": "watch",
            "action": "inspect_truncated_tool_outputs",
            "reason": "Typed envelopes report truncated tool output.",
            "evidence": evidence,
        }
    return {
        "status": "pass",
        "action": "keep_tool_envelope_contract",
        "reason": "All observed tool calls have typed non-error envelopes.",
        "evidence": evidence,
    }


def _tool_envelope_gate_blockers(agg: Dict[str, Any]) -> List[str]:
    plan = agg.get("tool_envelope_plan") or {}
    if not plan:
        return ["missing_tool_envelope_plan"]
    blockers = []
    for arm, item in sorted(plan.items()):
        status = str((item or {}).get("status") or "missing")
        if status != "pass":
            action = str((item or {}).get("action") or "unknown")
            blockers.append(f"{arm}:{status}:{action}")
    return blockers


def _split_feedback_gate_arms(values: Sequence[str]) -> List[str]:
    arms: List[str] = []
    seen = set()
    for value in values:
        for part in str(value).split(","):
            arm = part.strip()
            if not arm or arm in seen:
                continue
            seen.add(arm)
            arms.append(arm)
    return arms


def _merge_unique(*groups: Sequence[str]) -> List[str]:
    merged: List[str] = []
    seen = set()
    for group in groups:
        for item in group:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def _max_optional_float(values: Sequence[Optional[float]]) -> Optional[float]:
    present = [float(value) for value in values if value is not None]
    return max(present) if present else None


def _promotion_profile_requirements(
    profile_names: Sequence[str],
    *,
    feedback_arms: Sequence[str],
    feedback_require_savings: bool,
    answer_evidence_arms: Sequence[str],
    replay_arms: Sequence[str],
    answer_min_trace_rate: float,
    answer_min_read_rate: Optional[float],
    answer_max_unconfirmed: float,
    require_route_lifecycle: bool,
    require_tool_envelope_health: bool,
) -> Dict[str, Any]:
    profiles = [PROMOTION_PROFILES[name] for name in profile_names]
    profile_feedback_arms = [
        arm for profile in profiles for arm in profile.get("feedback_arms", [])
    ]
    profile_answer_arms = [
        arm for profile in profiles for arm in profile.get("answer_evidence_arms", [])
    ]
    profile_replay_arms = [
        arm for profile in profiles for arm in profile.get("replay_arms", [])
    ]
    profile_trace_rates = [
        profile.get("answer_evidence_min_trace_rate") for profile in profiles
    ]
    profile_read_rates = [
        profile.get("answer_evidence_min_read_rate") for profile in profiles
    ]
    profile_unconfirmed = [
        profile.get("answer_evidence_max_unconfirmed") for profile in profiles
    ]

    return {
        "profiles": list(profile_names),
        "feedback_arms": _merge_unique(feedback_arms, profile_feedback_arms),
        "feedback_require_savings": bool(
            feedback_require_savings
            or any(profile.get("feedback_require_savings") for profile in profiles)
        ),
        "answer_evidence_arms": _merge_unique(
            answer_evidence_arms, profile_answer_arms
        ),
        "replay_arms": _merge_unique(replay_arms, profile_replay_arms),
        "answer_evidence_min_trace_rate": max(
            [float(answer_min_trace_rate)]
            + [float(rate) for rate in profile_trace_rates if rate is not None]
        ),
        "answer_evidence_min_read_rate": _max_optional_float(
            [answer_min_read_rate, *profile_read_rates]
        ),
        "answer_evidence_max_unconfirmed": min(
            [float(answer_max_unconfirmed)]
            + [float(value) for value in profile_unconfirmed if value is not None]
        ),
        "require_route_lifecycle_gate": bool(
            require_route_lifecycle
            or any(profile.get("route_lifecycle") for profile in profiles)
        ),
        "require_tool_envelope_health": bool(
            require_tool_envelope_health
            or any(profile.get("tool_envelope_health") for profile in profiles)
        ),
    }


def _feedback_iteration_gate(
    agg: Dict[str, Any],
    arms: Sequence[str],
    *,
    require_savings: bool = False,
) -> Dict[str, Any]:
    """Evaluate whether feedback-probe arms are safe to promote."""

    by_arm = agg.get("by_arm") or {}
    paired = agg.get("paired") or {}
    categories = sorted(
        {
            key.split("/", 1)[0]
            for key in paired
            if isinstance(key, str) and "/" in key and not key.startswith("ALL/")
        }
    )
    runs: List[Dict[str, Any]] = []
    blockers: List[str] = []

    for arm in arms:
        run_blockers: List[str] = []
        all_row = paired.get(f"ALL/{arm}") or {}
        n = all_row.get("n")
        delta_rec = all_row.get("delta_rec5")
        delta_tokens = all_row.get("delta_tokens")
        delta_turns = all_row.get("delta_turns")

        if arm not in by_arm:
            run_blockers.append(f"{arm}:missing_arm")
        if n is None or int(n or 0) <= 0:
            run_blockers.append(f"{arm}:missing_paired_baseline")
        if delta_rec is None:
            run_blockers.append(f"{arm}:missing_delta_rec5")
        elif float(delta_rec) < -EPS:
            run_blockers.append(f"{arm}:rec5_regression")
        if require_savings and not (
            (delta_tokens is not None and float(delta_tokens) < 0)
            or (delta_turns is not None and float(delta_turns) < 0)
        ):
            run_blockers.append(f"{arm}:no_token_or_turn_savings")

        category_rows: List[Dict[str, Any]] = []
        for category in categories:
            row = paired.get(f"{category}/{arm}") or {}
            row_n = int(row.get("n") or 0)
            if row_n <= 0:
                continue
            row_delta = row.get("delta_rec5")
            row_blockers: List[str] = []
            if row_delta is None:
                row_blockers.append(f"{arm}:{category}:missing_delta_rec5")
            elif float(row_delta) < -EPS:
                row_blockers.append(f"{arm}:{category}:rec5_regression")
            run_blockers.extend(row_blockers)
            category_rows.append(
                {
                    "category": category,
                    "n": row_n,
                    "delta_rec5": row_delta,
                    "delta_tokens": row.get("delta_tokens"),
                    "delta_turns": row.get("delta_turns"),
                    "blockers": row_blockers,
                }
            )

        blockers.extend(run_blockers)
        runs.append(
            {
                "arm": arm,
                "status": "pass" if not run_blockers else "fail",
                "blockers": run_blockers,
                "evidence": {
                    "n": n,
                    "delta_rec5": delta_rec,
                    "delta_tokens": delta_tokens,
                    "delta_turns": delta_turns,
                },
                "category_evidence": category_rows,
            }
        )

    return {
        "gate": "feedback_iteration_promotion",
        "status": "pass" if not blockers else "fail",
        "promote": not blockers,
        "blockers": blockers,
        "criteria": {
            "baseline": BASELINE,
            "arms": list(arms),
            "max_rec5_regression": EPS,
            "require_savings": require_savings,
            "category_regressions_block": True,
        },
        "runs": runs,
    }


def _answer_evidence_gate(
    agg: Dict[str, Any],
    arms: Sequence[str],
    *,
    min_trace_rate: float = 1.0,
    min_read_rate: Optional[float] = None,
    max_unconfirmed: float = 0.0,
) -> Dict[str, Any]:
    """Evaluate whether committed answers are backed by trace evidence."""

    by_arm = agg.get("by_arm") or {}
    runs: List[Dict[str, Any]] = []
    blockers: List[str] = []

    for arm in arms:
        row = by_arm.get(arm) or {}
        run_blockers: List[str] = []
        span_min = row.get("answer_span_evidence_count_min")
        trace_min = row.get("answer_trace_evidence_rate_min")
        read_min = row.get("answer_read_confirmed_rate_min")
        unconfirmed_max = row.get("answer_unconfirmed_max")

        if arm not in by_arm:
            run_blockers.append(f"{arm}:missing_arm")
        if span_min is None or float(span_min) <= 0:
            run_blockers.append(f"{arm}:missing_answer_spans")
        if trace_min is None:
            run_blockers.append(f"{arm}:missing_trace_evidence_rate")
        elif float(trace_min) + 1e-9 < min_trace_rate:
            run_blockers.append(f"{arm}:trace_evidence_below_threshold")
        if min_read_rate is not None:
            if read_min is None:
                run_blockers.append(f"{arm}:missing_read_evidence_rate")
            elif float(read_min) + 1e-9 < min_read_rate:
                run_blockers.append(f"{arm}:read_evidence_below_threshold")
        if unconfirmed_max is None:
            run_blockers.append(f"{arm}:missing_unconfirmed_span_count")
        elif float(unconfirmed_max) > max_unconfirmed + 1e-9:
            run_blockers.append(f"{arm}:unconfirmed_answer_spans")

        blockers.extend(run_blockers)
        runs.append(
            {
                "arm": arm,
                "status": "pass" if not run_blockers else "fail",
                "blockers": run_blockers,
                "evidence": {
                    "n": row.get("n"),
                    "answer_span_evidence_count": row.get("answer_span_evidence_count"),
                    "answer_span_evidence_count_min": span_min,
                    "answer_trace_evidence_rate": row.get("answer_trace_evidence_rate"),
                    "answer_trace_evidence_rate_min": trace_min,
                    "answer_read_confirmed_rate": row.get("answer_read_confirmed_rate"),
                    "answer_read_confirmed_rate_min": read_min,
                    "answer_unconfirmed": row.get("answer_unconfirmed"),
                    "answer_unconfirmed_max": unconfirmed_max,
                },
            }
        )

    return {
        "gate": "answer_evidence_promotion",
        "status": "pass" if not blockers else "fail",
        "promote": not blockers,
        "blockers": blockers,
        "criteria": {
            "arms": list(arms),
            "min_trace_evidence_rate": min_trace_rate,
            "min_read_confirmed_rate": min_read_rate,
            "max_unconfirmed_spans": max_unconfirmed,
            "uses_min_max_not_mean": True,
        },
        "runs": runs,
    }


def _replay_readiness_gate(
    agg: Dict[str, Any],
    arms: Sequence[str],
) -> Dict[str, Any]:
    """Evaluate whether promoted cells carry exact replay artifacts."""

    by_arm = agg.get("by_arm") or {}
    runs: List[Dict[str, Any]] = []
    blockers: List[str] = []

    for arm in arms:
        row = by_arm.get(arm) or {}
        run_blockers: List[str] = []
        event_min = row.get("replay_event_replayable_min")
        exact_min = row.get("replay_exact_replayable_min")
        gap_max = row.get("replay_blocking_gap_count_max")
        gap_labels = [str(gap) for gap in row.get("replay_blocking_gap_labels") or []]

        if arm not in by_arm:
            run_blockers.append(f"{arm}:missing_arm")
        if event_min is None:
            run_blockers.append(f"{arm}:missing_event_replay_metric")
        elif float(event_min) < 1.0:
            run_blockers.append(f"{arm}:event_not_replayable")
        if exact_min is None:
            run_blockers.append(f"{arm}:missing_exact_replay_metric")
        elif float(exact_min) < 1.0:
            run_blockers.append(f"{arm}:exact_replay_blocked")
        if gap_max is None:
            run_blockers.append(f"{arm}:missing_replay_gap_metric")
        elif float(gap_max) > 0.0:
            run_blockers.extend(f"{arm}:{gap}" for gap in gap_labels)

        blockers.extend(run_blockers)
        runs.append(
            {
                "arm": arm,
                "status": "pass" if not run_blockers else "fail",
                "blockers": run_blockers,
                "evidence": {
                    "n": row.get("n"),
                    "event_replayable": row.get("replay_event_replayable"),
                    "event_replayable_min": event_min,
                    "exact_replayable": row.get("replay_exact_replayable"),
                    "exact_replayable_min": exact_min,
                    "has_message_transcript": row.get("replay_has_message_transcript"),
                    "message_count": row.get("replay_message_count"),
                    "tool_schema_count": row.get("replay_tool_schema_count"),
                    "blocking_gap_count": row.get("replay_blocking_gap_count"),
                    "blocking_gap_count_max": gap_max,
                    "blocking_gap_labels": gap_labels,
                },
            }
        )

    return {
        "gate": "replay_readiness_promotion",
        "status": "pass" if not blockers else "fail",
        "promote": not blockers,
        "blockers": blockers,
        "criteria": {
            "arms": list(arms),
            "require_event_replayable": True,
            "require_exact_message_replayable": True,
            "max_blocking_gaps": 0,
        },
        "runs": runs,
    }


def _gate_run_blockers(gate: Dict[str, Any], arm: str) -> List[str]:
    for run in gate.get("runs") or []:
        if (run or {}).get("arm") == arm:
            return [str(blocker) for blocker in (run or {}).get("blockers") or []]
    return [f"{arm}:missing_gate_run"]


def _promotion_profile_gate(
    agg: Dict[str, Any],
    profile_names: Sequence[str],
) -> Dict[str, Any]:
    runs: List[Dict[str, Any]] = []
    blockers: List[str] = []

    for name in profile_names:
        profile = PROMOTION_PROFILES[name]
        run_blockers: List[str] = []
        gates: List[str] = []

        feedback_arms = profile.get("feedback_arms") or []
        if feedback_arms:
            gates.append("feedback_iteration")
            feedback_gate = agg.get("feedback_iteration_gate") or {}
            if not feedback_gate:
                run_blockers.append(f"{name}:missing_feedback_iteration_gate")
            else:
                for arm in feedback_arms:
                    for blocker in _gate_run_blockers(feedback_gate, str(arm)):
                        run_blockers.append(f"{name}:feedback:{blocker}")

        answer_arms = profile.get("answer_evidence_arms") or []
        if answer_arms:
            gates.append("answer_evidence")
            answer_gate = agg.get("answer_evidence_gate") or {}
            if not answer_gate:
                run_blockers.append(f"{name}:missing_answer_evidence_gate")
            else:
                for arm in answer_arms:
                    for blocker in _gate_run_blockers(answer_gate, str(arm)):
                        run_blockers.append(f"{name}:answer_evidence:{blocker}")

        replay_arms = profile.get("replay_arms") or []
        if replay_arms:
            gates.append("replay_readiness")
            replay_gate = agg.get("replay_readiness_gate") or {}
            if not replay_gate:
                run_blockers.append(f"{name}:missing_replay_readiness_gate")
            else:
                for arm in replay_arms:
                    for blocker in _gate_run_blockers(replay_gate, str(arm)):
                        run_blockers.append(f"{name}:replay:{blocker}")

        if profile.get("route_lifecycle"):
            gates.append("route_lifecycle")
            route_gate = agg.get("route_lifecycle_gate") or {}
            if not route_gate.get("promote"):
                route_blockers = route_gate.get("blockers") or [
                    route_gate.get("reason") or route_gate.get("status") or "unknown"
                ]
                for blocker in route_blockers:
                    run_blockers.append(f"{name}:route_lifecycle:{blocker}")

        if profile.get("tool_envelope_health"):
            gates.append("tool_envelope_health")
            for blocker in _tool_envelope_gate_blockers(agg):
                run_blockers.append(f"{name}:tool_envelope:{blocker}")

        blockers.extend(run_blockers)
        runs.append(
            {
                "profile": name,
                "status": "pass" if not run_blockers else "fail",
                "blockers": run_blockers,
                "gates": gates,
                "description": profile.get("description"),
            }
        )

    return {
        "gate": "promotion_profile",
        "status": "pass" if not blockers else "fail",
        "promote": not blockers,
        "profiles": list(profile_names),
        "blockers": blockers,
        "runs": runs,
        "available_profiles": {
            name: profile.get("description")
            for name, profile in sorted(PROMOTION_PROFILES.items())
        },
    }


def _promotion_blocker_actions(blockers: Sequence[str]) -> List[str]:
    actions: List[str] = []
    rules = [
        (
            "complete_replay_artifacts",
            (
                "replay",
                "exact_replay",
                "message_transcript",
                "tool_result_content",
                "llm_call",
            ),
        ),
        (
            "repair_answer_evidence",
            ("answer_evidence", "trace_evidence", "unconfirmed_answer", "answer_spans"),
        ),
        (
            "restore_answer_recall",
            ("rec5_regression", "missing_delta_rec5"),
        ),
        (
            "restore_paired_baseline_coverage",
            ("missing_paired_baseline", "missing_arm", "insufficient"),
        ),
        (
            "reduce_tokens_or_turns",
            ("no_token_or_turn_savings",),
        ),
        (
            "revise_route_lifecycle",
            ("route_lifecycle", "lsp_route", "route_"),
        ),
        (
            "repair_tool_envelopes",
            ("tool_envelope", "typed_tool", "tool_"),
        ),
    ]
    for action, needles in rules:
        if any(any(needle in blocker for needle in needles) for blocker in blockers):
            actions.append(action)
    if blockers and not actions:
        actions.append("revise_harness_slice")
    return actions


def _promotion_decision(
    agg: Dict[str, Any],
    profile_names: Sequence[str],
) -> Dict[str, Any]:
    profile_gate = agg.get("promotion_profile_gate") or {}
    blockers = [str(blocker) for blocker in profile_gate.get("blockers") or []]
    next_actions = _promotion_blocker_actions(blockers)
    if not profile_names:
        return {
            "decision": "not_configured",
            "promote": False,
            "action": "run_with_promotion_profile",
            "reason": "No promotion profile was requested.",
            "profiles": [],
            "blockers": [],
            "next_actions": ["run_with_promotion_profile"],
        }
    if profile_gate.get("promote"):
        return {
            "decision": "promote",
            "promote": True,
            "action": "promote_harness_slice",
            "reason": "All requested promotion profiles passed.",
            "profiles": list(profile_names),
            "blockers": [],
            "next_actions": [],
            "profile_gate": {
                "status": profile_gate.get("status"),
                "gate": profile_gate.get("gate"),
            },
        }
    return {
        "decision": "revise",
        "promote": False,
        "action": next_actions[0] if next_actions else "revise_harness_slice",
        "reason": "One or more promotion profile gates failed.",
        "profiles": list(profile_names),
        "blockers": blockers,
        "next_actions": next_actions,
        "profile_gate": {
            "status": profile_gate.get("status"),
            "gate": profile_gate.get("gate"),
        },
    }


def _answer_diagnosis_plan(
    row: Dict[str, Any], paired: Dict[str, Any]
) -> Dict[str, Any]:
    rates = {
        label: float(row.get(f"answer_diag_{label}") or 0.0)
        for label in ANSWER_DIAGNOSES
    }
    dominant, rate = max(rates.items(), key=lambda item: item[1])
    evidence = {
        "format_failed": row.get("format_failed"),
        "rec5": row.get("rec5"),
        "answer_rec_all": row.get("answer_rec_all"),
        "answer_path_alias_rec_all": row.get("answer_path_alias_rec_all"),
        "delta_rec5": paired.get("delta_rec5"),
        "delta_answer_rec_all": paired.get("delta_answer_rec_all"),
        "delta_answer_path_alias_rec_all": paired.get(
            "delta_answer_path_alias_rec_all"
        ),
        "delta_tokens": paired.get("delta_tokens"),
        "diagnosis_rates": rates,
    }
    if rate <= 0:
        return {
            "dominant": "n/a",
            "rate": None,
            "rec5": row.get("rec5"),
            "answer_rec_all": row.get("answer_rec_all"),
            "answer_path_alias_rec_all": row.get("answer_path_alias_rec_all"),
            "action": "no_answer_diagnosis",
            "reason": "No answer diagnosis signal was available for this arm.",
            "evidence": evidence,
        }

    if dominant == "ok":
        action = "keep_answer_contract"
        reason = "Most parsed answers satisfy the strict span scorer."
    elif dominant == "rank_gap":
        action = "tighten_answer_ordering"
        reason = "The answer contains target spans, but they fall outside top-k."
    elif dominant == "path_alias_gap":
        action = "normalize_repo_relative_paths"
        reason = "The answer cites basename-equivalent paths, not strict repo-relative paths."
    elif dominant in {"format_gap", "empty_answer"}:
        action = "strengthen_schema_repair"
        reason = "The run produced no parseable final Locations span."
    else:
        action = "improve_context_routing"
        reason = "The committed answer spans do not overlap the ground-truth content."

    return {
        "dominant": dominant,
        "rate": rate,
        "rec5": row.get("rec5"),
        "answer_rec_all": row.get("answer_rec_all"),
        "answer_path_alias_rec_all": row.get("answer_path_alias_rec_all"),
        "action": action,
        "reason": reason,
        "evidence": evidence,
    }


def _route_lifecycle_gate(
    by_arm: Dict[str, Dict[str, Any]], paired: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Evaluate the M3.5bb scheduled route lifecycle promotion gate."""

    arm = ROUTE_LIFECYCLE_ARM
    row = by_arm.get(arm)
    evidence: Dict[str, Any] = {"arm": arm}
    if not row:
        return {
            "gate": "M3.5bb_route_lifecycle_repeat",
            "arm": arm,
            "status": "missing",
            "promote": False,
            "action": "run_route_scheduler_repeat_gate",
            "blockers": ["missing_preinj_graph_scheduled_arm"],
            "reason": "No scheduled route arm was present in this aggregate.",
            "evidence": evidence,
        }

    paired_all = paired.get(f"ALL/{arm}", {})
    evidence.update(
        {
            "n": row.get("n"),
            "reps": row.get("reps"),
            "reps_min": row.get("reps_min"),
            "paired_n": paired_all.get("n"),
            "rec_min": row.get("rec5_min"),
            "rec5": row.get("rec5"),
            "files5": row.get("files5"),
            "tokens": row.get("tokens"),
            "turns": row.get("turns"),
            "delta_rec5": paired_all.get("delta_rec5"),
            "delta_tokens": paired_all.get("delta_tokens"),
            "delta_turns": paired_all.get("delta_turns"),
            "route": row.get("scheduled_lsp_route"),
            "scheduled_graph_fanout": row.get("scheduled_graph_fanout"),
            "top_anchor_audit": row.get("scheduled_top_anchor_audit"),
            "top_anchor_validated_deterministic": row.get(
                "scheduled_top_anchor_audit_validated_deterministic"
            ),
            "top_anchor_unvalidated": row.get("scheduled_top_anchor_audit_unvalidated"),
            "generic_min_reads": row.get("scheduled_route_generic_min_reads"),
            "route_fanout_hold_count": row.get("scheduled_route_fanout_hold_count"),
            "route_fanout_hold_first_turn": row.get(
                "scheduled_route_fanout_hold_first_turn"
            ),
            "route_fanout_hold_read_calls": row.get(
                "scheduled_route_fanout_hold_read_calls"
            ),
        }
    )

    blockers: List[str] = []
    incomplete_blockers: List[str] = []
    failure_blockers: List[str] = []
    n = row.get("n")
    if n is None:
        incomplete_blockers.append("missing_query_coverage")
    elif int(n) < ROUTE_LIFECYCLE_MIN_QUERIES:
        incomplete_blockers.append("insufficient_query_coverage")

    paired_n = paired_all.get("n")
    if paired_n is None:
        incomplete_blockers.append("missing_paired_baseline_coverage")
    elif int(paired_n) < ROUTE_LIFECYCLE_MIN_QUERIES:
        incomplete_blockers.append("insufficient_paired_baseline_coverage")

    reps_min = row.get("reps_min")
    if reps_min is None:
        incomplete_blockers.append("missing_repeat_coverage")
    elif float(reps_min) < ROUTE_LIFECYCLE_MIN_REPS:
        incomplete_blockers.append("insufficient_repeat_coverage")

    rec_min = row.get("rec5_min")
    if rec_min is None:
        incomplete_blockers.append("missing_rec_min")
    elif float(rec_min) < 1.0 - 1e-9:
        failure_blockers.append("rec_min_below_1.0")

    route = row.get("scheduled_lsp_route")
    if route is None:
        incomplete_blockers.append("missing_route_metric")
    elif float(route) < 1.0 - 1e-9:
        failure_blockers.append("lsp_route_not_all_cells")

    sfan = row.get("scheduled_graph_fanout")
    if sfan is None:
        incomplete_blockers.append("missing_graph_fanout_metric")
    elif float(sfan) > 0.0:
        failure_blockers.append("search_fanout_preempted_route")

    top_anchor_audit = row.get("scheduled_top_anchor_audit")
    if top_anchor_audit is None:
        incomplete_blockers.append("missing_top_anchor_audit_metric")
    elif float(top_anchor_audit) > 0.0:
        top_anchor_unvalidated = row.get("scheduled_top_anchor_audit_unvalidated")
        if top_anchor_unvalidated is None:
            incomplete_blockers.append("missing_top_anchor_validation_metric")
        elif float(top_anchor_unvalidated) > 0.0:
            failure_blockers.append("top_anchor_unvalidated_correction")

    generic_min_reads = row.get("scheduled_route_generic_min_reads")
    if generic_min_reads is None:
        incomplete_blockers.append("missing_generic_min_reads")
    elif float(generic_min_reads) < 4.0:
        failure_blockers.append("generic_route_threshold_below_4")

    blockers = incomplete_blockers + failure_blockers
    if failure_blockers:
        action = "revise_route_lifecycle_policy"
        reason = "M3.5bb route lifecycle gate failed: " + ", ".join(blockers)
        status = "fail"
    elif incomplete_blockers:
        action = "rerun_route_scheduler_repeat_gate"
        reason = "M3.5bb route lifecycle gate is incomplete: " + ", ".join(
            incomplete_blockers
        )
        status = "incomplete"
    else:
        action = "promote_route_lifecycle_slice"
        reason = (
            "Scheduled lsp_route repeated cleanly with full recall, no unsafe "
            "top-anchor correction, no graph-fanout preemption, and a stable "
            "generic read gate."
        )
        status = "pass"

    return {
        "gate": "M3.5bb_route_lifecycle_repeat",
        "arm": arm,
        "status": status,
        "promote": status == "pass",
        "action": action,
        "blockers": blockers,
        "incomplete_blockers": incomplete_blockers,
        "failure_blockers": failure_blockers,
        "reason": reason,
        "evidence": evidence,
    }


def _csv_set(value: Optional[str]) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _route_lifecycle_config_preflight(
    config_path: Path,
    *,
    synthesis_configs: Optional[str] = None,
    categories: Optional[str] = None,
    max_queries_per_category: Optional[int] = None,
) -> Dict[str, Any]:
    """Check whether a planned route-lifecycle run can satisfy M3.5bb."""

    from scripts.agent_compile.lib.config import SweepConfig

    cfg = SweepConfig.from_yaml(config_path)
    incomplete_blockers: List[str] = []
    failure_blockers: List[str] = []
    subsets = cfg.subsets or {}
    preload = cfg.preload or {}
    route_preload = preload.get(ROUTE_LIFECYCLE_ARM) or {}
    configs = _csv_set(synthesis_configs)
    cats = _csv_set(categories)
    expected_queries = (
        len(cfg.instances or []) * int(max_queries_per_category or 0)
        if max_queries_per_category
        else None
    )

    if BASELINE not in subsets:
        incomplete_blockers.append("missing_grep_only_arm")
    if ROUTE_LIFECYCLE_ARM not in subsets:
        incomplete_blockers.append("missing_preinj_graph_scheduled_arm")
    if float(cfg.reps or 0) < ROUTE_LIFECYCLE_MIN_REPS:
        incomplete_blockers.append("insufficient_repeat_config")
    if len(cfg.instances or []) < 2:
        incomplete_blockers.append("insufficient_instance_coverage")
    if expected_queries is None:
        incomplete_blockers.append("missing_max_queries_per_category")
    elif expected_queries < ROUTE_LIFECYCLE_MIN_QUERIES:
        incomplete_blockers.append("insufficient_query_selector")
    if not configs:
        incomplete_blockers.append("missing_synthesis_config_selector")
    elif not {"Go", "Rust"}.issubset(configs):
        incomplete_blockers.append("missing_go_rust_synthesis_configs")
    if not cats:
        incomplete_blockers.append("missing_category_selector")
    elif "traversal" not in cats:
        incomplete_blockers.append("missing_traversal_category")
    if not route_preload:
        incomplete_blockers.append("missing_scheduled_preload")
    else:
        if not route_preload.get("graph_schedule_on_read_symbols"):
            incomplete_blockers.append("read_symbol_scheduler_disabled")
        if not route_preload.get("graph_schedule_on_fanout"):
            incomplete_blockers.append("fanout_scheduler_disabled")
        if not route_preload.get("graph_read_symbol_lsp_route", True):
            incomplete_blockers.append("lsp_route_disabled")
        if not route_preload.get(
            "graph_read_symbol_hold_fanout_for_route_queries", True
        ):
            failure_blockers.append("route_fanout_hold_disabled")
        generic_min = route_preload.get("graph_generic_read_symbol_min_reads")
        if generic_min is None:
            incomplete_blockers.append("missing_generic_min_reads_config")
        else:
            try:
                if int(generic_min) < 4:
                    failure_blockers.append("generic_route_threshold_below_4_config")
            except (TypeError, ValueError):
                incomplete_blockers.append("invalid_generic_min_reads_config")

    blockers = incomplete_blockers + failure_blockers
    if failure_blockers:
        status = "fail"
        action = "fix_route_lifecycle_config"
        reason = "M3.5bb route lifecycle preflight failed: " + ", ".join(blockers)
    elif incomplete_blockers:
        status = "incomplete"
        action = "complete_route_lifecycle_run_config"
        reason = "M3.5bb route lifecycle preflight is incomplete: " + ", ".join(
            incomplete_blockers
        )
    else:
        status = "pass"
        action = "run_route_scheduler_repeat_gate"
        reason = "Route scheduler config and selectors can produce the M3.5bb gate."

    return {
        "gate": "M3.5bb_route_lifecycle_config_preflight",
        "config": str(config_path),
        "status": status,
        "ready": status == "pass",
        "action": action,
        "blockers": blockers,
        "incomplete_blockers": incomplete_blockers,
        "failure_blockers": failure_blockers,
        "reason": reason,
        "evidence": {
            "sweep_id": cfg.sweep_id,
            "reps": cfg.reps,
            "instances": cfg.instances,
            "subsets": sorted(subsets),
            "synthesis_configs": sorted(configs),
            "categories": sorted(cats),
            "max_queries_per_category": max_queries_per_category,
            "expected_query_coverage": expected_queries,
            "graph_generic_read_symbol_min_reads": route_preload.get(
                "graph_generic_read_symbol_min_reads"
            ),
            "graph_read_symbol_hold_fanout_for_route_queries": route_preload.get(
                "graph_read_symbol_hold_fanout_for_route_queries", True
            ),
        },
    }


def render(agg: Dict[str, Any]) -> str:
    lines = ["# Agent harness feedback probe", ""]
    lines.append(
        "Small-sample control panel. Use this to compare harness iterations, "
        "not as a final dataset result. `rec@5` is committed answer-block recall; "
        "`trace%` shows whether new cells carried trace data."
    )
    lines.append("")
    head = [
        "arm",
        "n",
        "rec@5",
        "files@5",
        "tokens",
        "turns",
        "rep_n",
        "rec_min",
        "tok_hi",
        "tok_rng",
        "turn_hi",
        "turn_rng",
        "fmt_fail",
        "diag",
        "alias_norm%",
        "alias_rep",
        "schema_salv%",
        "schema_loc",
        "ord_norm%",
        "ord_prom",
        "reads",
        "tool_n",
        "tenv%",
        "terr",
        "tskip",
        "ttrunc",
        "tchars_k",
        "rrep",
        "rovlp%",
        "rovlp_l",
        "rov_pre_l",
        "rov_post_l",
        "rov_fg_l",
        "post_sub",
        "post_exp",
        "post_shift",
        "post_low",
        "rejected",
        "preload",
        "pre_vfy%",
        "ans",
        "te%",
        "te_min%",
        "rc%",
        "rc_min%",
        "unc",
        "unc_max",
        "summary",
        "pruned_k",
        "seed_k",
        "tail_k",
        "cprune%",
        "tail%",
        "keep",
        "cpol",
        "comp%",
        "fb%",
        "gexp%",
        "gexp_n",
        "lsp",
        "lsp%",
        "lr%",
        "lrturn",
        "lrres",
        "sched%",
        "sched_n",
        "route%",
        "rturn",
        "rcalls",
        "rgmin",
        "rbridge%",
        "rhold",
        "rhturn",
        "rhcalls",
        "rb%",
        "rb_drop",
        "rb_unsafe%",
        "rb_miss",
        "rread",
        "rread_c",
        "rkeep",
        "runread_c",
        "runkeep",
        "runans",
        "runans5",
        "runr5",
        "runrtail",
        "sdef%",
        "fg%",
        "fg_n",
        "sfan%",
        "op?%",
        "aud%",
        "sskip%",
        "taud%",
        "taud_m",
        "taud_u",
        "taud_ao%",
        "taud_bt%",
        "taud_dp%",
        "taud_vdp%",
        "taud_bad%",
        "taud_ro%",
        "taud_fb%",
        "ord%",
        "ord_n",
        "no_read",
        "trace%",
    ]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("| " + " | ".join("---" for _ in head) + " |")
    for arm, row in sorted(agg["by_arm"].items()):
        lines.append(
            "| "
            + " | ".join(
                [
                    arm,
                    str(row["n"]),
                    _fmt(row.get("rec5")),
                    _fmt(row.get("files5")),
                    _fmt(row.get("tokens"), 0),
                    _fmt(row.get("turns"), 1),
                    _fmt(row.get("reps"), 1),
                    _fmt(row.get("rec5_min")),
                    _fmt(row.get("tokens_max"), 0),
                    _fmt(row.get("tokens_spread"), 0),
                    _fmt(row.get("turns_max"), 1),
                    _fmt(row.get("turns_spread"), 1),
                    _pct(row.get("format_failed")),
                    _answer_diagnosis_mix(row),
                    _pct(row.get("answer_path_alias_normalized")),
                    _fmt(row.get("answer_path_alias_replacements"), 1),
                    _pct(row.get("answer_schema_salvaged")),
                    _fmt(row.get("answer_schema_salvage_locations"), 1),
                    _pct(row.get("answer_location_order_normalized")),
                    _fmt(row.get("answer_location_order_promotions"), 1),
                    _fmt(row.get("reads"), 1),
                    _fmt(row.get("tool_calls"), 1),
                    _pct(row.get("tool_envelope_coverage")),
                    _fmt(row.get("tool_errors"), 1),
                    _fmt(row.get("tool_skipped"), 1),
                    _fmt(row.get("tool_truncated"), 1),
                    _fmt((row.get("tool_result_chars") or 0) / 1000, 1),
                    _fmt(row.get("read_repeat_offsets"), 1),
                    _pct(row.get("read_overlap_rate")),
                    _fmt(row.get("read_overlap_lines"), 1),
                    _fmt(row.get("read_overlap_pre_route_lines"), 1),
                    _fmt(row.get("read_overlap_post_route_lines"), 1),
                    _fmt(row.get("read_overlap_post_guard_lines"), 1),
                    _fmt(row.get("read_overlap_post_route_contained_events"), 1),
                    _fmt(row.get("read_overlap_post_route_expansion_events"), 1),
                    _fmt(row.get("read_overlap_post_route_shifted_events"), 1),
                    _fmt(row.get("read_overlap_post_route_low_novelty_events"), 1),
                    _fmt(row.get("rejected"), 1),
                    _fmt(row.get("preload"), 1),
                    _pct(row.get("preload_verify_rate")),
                    _fmt(row.get("answer_span_evidence_count"), 1),
                    _pct(row.get("answer_trace_evidence_rate")),
                    _pct(row.get("answer_trace_evidence_rate_min")),
                    _pct(row.get("answer_read_confirmed_rate")),
                    _pct(row.get("answer_read_confirmed_rate_min")),
                    _fmt(row.get("answer_unconfirmed"), 1),
                    _fmt(row.get("answer_unconfirmed_max"), 1),
                    _fmt(row.get("summarized"), 1),
                    _fmt((row.get("pruned_tool_chars") or 0) / 1000, 1),
                    _fmt((row.get("seed_chars") or 0) / 1000, 1),
                    _fmt((row.get("tail_chars") or 0) / 1000, 1),
                    _pct(row.get("compact_prune_ratio")),
                    _pct(row.get("compact_tail_share")),
                    _fmt(row.get("compact_keep_reads"), 1),
                    str((agg.get("compact_policy") or {}).get(arm) or "n/a"),
                    _pct(row.get("compacted")),
                    _pct(row.get("fallback")),
                    _pct(row.get("graph_expansion")),
                    _fmt(row.get("graph_expansion_nodes"), 1),
                    _fmt(row.get("lsp"), 1),
                    _pct(row.get("lsp_used")),
                    _pct(row.get("lsp_route_tool_used")),
                    _fmt(row.get("lsp_route_tool_first_turn"), 1),
                    _fmt(row.get("lsp_route_tool_results"), 1),
                    _pct(row.get("scheduled_context")),
                    _fmt(row.get("scheduled_context_nodes"), 1),
                    _pct(row.get("scheduled_lsp_route")),
                    _fmt(row.get("scheduled_route_trigger_turn"), 1),
                    _fmt(row.get("scheduled_route_read_calls"), 1),
                    _fmt(row.get("scheduled_route_generic_min_reads"), 1),
                    _pct(row.get("scheduled_route_bridge_query")),
                    _fmt(row.get("scheduled_route_fanout_hold_count"), 1),
                    _fmt(row.get("scheduled_route_fanout_hold_first_turn"), 1),
                    _fmt(row.get("scheduled_route_fanout_hold_read_calls"), 1),
                    _pct(row.get("scheduled_route_budget_applied")),
                    _fmt(row.get("scheduled_route_budget_dropped"), 1),
                    _pct(row.get("scheduled_route_budget_unsafe")),
                    _fmt(row.get("scheduled_route_budget_missing_required"), 1),
                    _fmt(row.get("scheduled_route_read_confirmed"), 1),
                    _fmt(row.get("scheduled_route_read_confirmed_core"), 1),
                    _fmt(row.get("scheduled_route_read_confirmed_protected"), 1),
                    _fmt(row.get("scheduled_route_unread_core"), 1),
                    _fmt(row.get("scheduled_route_unread_protected"), 1),
                    _fmt(row.get("scheduled_route_unread_core_answer_cited"), 1),
                    _fmt(row.get("scheduled_route_unread_core_answer_top5"), 1),
                    _fmt(
                        row.get("scheduled_route_unread_core_answer_route_top5"),
                        1,
                    ),
                    _fmt(
                        row.get("scheduled_route_unread_core_answer_route_tail"),
                        1,
                    ),
                    _pct(row.get("scheduled_lsp_definition")),
                    _pct(row.get("scheduled_final_guard")),
                    _fmt(row.get("scheduled_final_guard_nodes"), 1),
                    _pct(row.get("scheduled_graph_fanout")),
                    _pct(row.get("scheduled_unknown_operation")),
                    _pct(row.get("scheduled_anchor_audit")),
                    _pct(row.get("scheduled_context_skipped")),
                    _pct(row.get("scheduled_top_anchor_audit")),
                    _fmt(row.get("scheduled_top_anchor_audit_missing"), 1),
                    _fmt(row.get("scheduled_top_anchor_audit_missing_unread"), 1),
                    _pct(row.get("scheduled_top_anchor_audit_answer_only")),
                    _pct(row.get("scheduled_top_anchor_audit_bounded_tool_loop")),
                    _pct(row.get("scheduled_top_anchor_audit_deterministic_patch")),
                    _pct(row.get("scheduled_top_anchor_audit_validated_deterministic")),
                    _pct(row.get("scheduled_top_anchor_audit_unvalidated")),
                    _pct(row.get("scheduled_top_anchor_audit_read_only")),
                    _pct(row.get("scheduled_top_anchor_audit_cascade_fallback")),
                    _pct(row.get("scheduled_ordering_audit")),
                    _fmt(row.get("scheduled_ordering_audit_spans"), 1),
                    _pct(row.get("no_read")),
                    _pct(row.get("has_trace")),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Compact Policy Plan")
    lines.append("")
    head_policy = ["arm", "policy", "action", "patch", "reason"]
    lines.append("| " + " | ".join(head_policy) + " |")
    lines.append("| " + " | ".join("---" for _ in head_policy) + " |")
    for arm, plan in sorted((agg.get("compact_policy_plan") or {}).items()):
        policy = str(plan.get("policy") or "n/a")
        if policy == "n/a":
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    arm,
                    policy,
                    str(plan.get("action") or ""),
                    _compact_patch_text(plan),
                    str(plan.get("reason") or ""),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Answer Diagnosis Plan")
    lines.append("")
    head_answer = [
        "arm",
        "rec@5",
        "rec@all",
        "alias@all",
        "dominant",
        "rate",
        "action",
        "reason",
    ]
    lines.append("| " + " | ".join(head_answer) + " |")
    lines.append("| " + " | ".join("---" for _ in head_answer) + " |")
    for arm, plan in sorted((agg.get("answer_diagnosis_plan") or {}).items()):
        dominant = str(plan.get("dominant") or "n/a")
        if dominant == "n/a":
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    arm,
                    _fmt(plan.get("rec5")),
                    _fmt(plan.get("answer_rec_all")),
                    _fmt(plan.get("answer_path_alias_rec_all")),
                    dominant,
                    _pct(plan.get("rate")),
                    str(plan.get("action") or ""),
                    str(plan.get("reason") or ""),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Tool Envelope Plan")
    lines.append("")
    head_tool = [
        "arm",
        "status",
        "action",
        "tool_n",
        "tenv%",
        "terr",
        "tskip",
        "ttrunc",
        "reason",
    ]
    lines.append("| " + " | ".join(head_tool) + " |")
    lines.append("| " + " | ".join("---" for _ in head_tool) + " |")
    for arm, plan in sorted((agg.get("tool_envelope_plan") or {}).items()):
        evidence = plan.get("evidence") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    arm,
                    str(plan.get("status") or "n/a"),
                    str(plan.get("action") or "n/a"),
                    _fmt(evidence.get("tool_calls"), 1),
                    _pct(evidence.get("tool_envelope_coverage")),
                    _fmt(evidence.get("tool_errors"), 1),
                    _fmt(evidence.get("tool_skipped"), 1),
                    _fmt(evidence.get("tool_truncated"), 1),
                    str(plan.get("reason") or ""),
                ]
            )
            + " |"
        )
    lines.append("")
    feedback_gate = agg.get("feedback_iteration_gate") or {}
    if feedback_gate:
        lines.append("## Feedback Iteration Gate")
        lines.append("")
        head_feedback = [
            "gate",
            "status",
            "arm",
            "n",
            "Δrec@5",
            "Δtokens",
            "Δturns",
            "blockers",
        ]
        lines.append("| " + " | ".join(head_feedback) + " |")
        lines.append("| " + " | ".join("---" for _ in head_feedback) + " |")
        for run in feedback_gate.get("runs") or []:
            evidence = run.get("evidence") or {}
            blockers = run.get("blockers") or []
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(feedback_gate.get("gate") or "n/a"),
                        str(run.get("status") or "n/a"),
                        str(run.get("arm") or "n/a"),
                        _fmt(evidence.get("n"), 0),
                        _fmt(evidence.get("delta_rec5")),
                        _fmt(evidence.get("delta_tokens"), 0),
                        _fmt(evidence.get("delta_turns"), 1),
                        ",".join(str(blocker) for blocker in blockers) or "-",
                    ]
                )
                + " |"
            )
        lines.append("")
    answer_gate = agg.get("answer_evidence_gate") or {}
    if answer_gate:
        lines.append("## Answer Evidence Gate")
        lines.append("")
        head_answer_gate = [
            "gate",
            "status",
            "arm",
            "n",
            "ans_min",
            "te_min%",
            "rc_min%",
            "unc_max",
            "blockers",
        ]
        lines.append("| " + " | ".join(head_answer_gate) + " |")
        lines.append("| " + " | ".join("---" for _ in head_answer_gate) + " |")
        for run in answer_gate.get("runs") or []:
            evidence = run.get("evidence") or {}
            blockers = run.get("blockers") or []
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(answer_gate.get("gate") or "n/a"),
                        str(run.get("status") or "n/a"),
                        str(run.get("arm") or "n/a"),
                        _fmt(evidence.get("n"), 0),
                        _fmt(evidence.get("answer_span_evidence_count_min"), 1),
                        _pct(evidence.get("answer_trace_evidence_rate_min")),
                        _pct(evidence.get("answer_read_confirmed_rate_min")),
                        _fmt(evidence.get("answer_unconfirmed_max"), 1),
                        ",".join(str(blocker) for blocker in blockers) or "-",
                    ]
                )
                + " |"
            )
        lines.append("")
    replay_gate = agg.get("replay_readiness_gate") or {}
    if replay_gate:
        lines.append("## Replay Readiness Gate")
        lines.append("")
        head_replay_gate = [
            "gate",
            "status",
            "arm",
            "n",
            "event_min%",
            "exact_min%",
            "msg",
            "schema",
            "gap_max",
            "gaps",
            "blockers",
        ]
        lines.append("| " + " | ".join(head_replay_gate) + " |")
        lines.append("| " + " | ".join("---" for _ in head_replay_gate) + " |")
        for run in replay_gate.get("runs") or []:
            evidence = run.get("evidence") or {}
            blockers = run.get("blockers") or []
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(replay_gate.get("gate") or "n/a"),
                        str(run.get("status") or "n/a"),
                        str(run.get("arm") or "n/a"),
                        _fmt(evidence.get("n"), 0),
                        _pct(evidence.get("event_replayable_min")),
                        _pct(evidence.get("exact_replayable_min")),
                        _fmt(evidence.get("message_count"), 1),
                        _fmt(evidence.get("tool_schema_count"), 1),
                        _fmt(evidence.get("blocking_gap_count_max"), 1),
                        ",".join(
                            str(gap)
                            for gap in evidence.get("blocking_gap_labels") or []
                        )
                        or "-",
                        ",".join(str(blocker) for blocker in blockers) or "-",
                    ]
                )
                + " |"
            )
        lines.append("")
    promotion_decision = agg.get("promotion_decision") or {}
    if promotion_decision:
        lines.append("## Promotion Decision")
        lines.append("")
        head_decision = [
            "decision",
            "promote",
            "action",
            "profiles",
            "next_actions",
            "blockers",
        ]
        lines.append("| " + " | ".join(head_decision) + " |")
        lines.append("| " + " | ".join("---" for _ in head_decision) + " |")
        lines.append(
            "| "
            + " | ".join(
                [
                    str(promotion_decision.get("decision") or "n/a"),
                    "yes" if promotion_decision.get("promote") else "no",
                    str(promotion_decision.get("action") or "n/a"),
                    ",".join(
                        str(profile)
                        for profile in promotion_decision.get("profiles") or []
                    )
                    or "-",
                    ",".join(
                        str(action)
                        for action in promotion_decision.get("next_actions") or []
                    )
                    or "-",
                    ",".join(
                        str(blocker)
                        for blocker in promotion_decision.get("blockers") or []
                    )
                    or "-",
                ]
            )
            + " |"
        )
        lines.append("")
    profile_gate = agg.get("promotion_profile_gate") or {}
    if profile_gate:
        lines.append("## Promotion Profile Gate")
        lines.append("")
        head_profile = ["gate", "status", "profile", "gates", "blockers"]
        lines.append("| " + " | ".join(head_profile) + " |")
        lines.append("| " + " | ".join("---" for _ in head_profile) + " |")
        for run in profile_gate.get("runs") or []:
            blockers = run.get("blockers") or []
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(profile_gate.get("gate") or "n/a"),
                        str(run.get("status") or "n/a"),
                        str(run.get("profile") or "n/a"),
                        ",".join(str(gate) for gate in run.get("gates") or []) or "-",
                        ",".join(str(blocker) for blocker in blockers) or "-",
                    ]
                )
                + " |"
            )
        lines.append("")
    lines.append("## Route Lifecycle Gate")
    lines.append("")
    head_route = [
        "gate",
        "arm",
        "status",
        "action",
        "n",
        "paired_n",
        "rep_min",
        "rec_min",
        "route%",
        "sfan%",
        "taud%",
        "taud_bad%",
        "rgmin",
        "rhold",
        "blockers",
    ]
    lines.append("| " + " | ".join(head_route) + " |")
    lines.append("| " + " | ".join("---" for _ in head_route) + " |")
    gate = agg.get("route_lifecycle_gate") or {}
    evidence = gate.get("evidence") or {}
    blockers = gate.get("blockers") or []
    lines.append(
        "| "
        + " | ".join(
            [
                str(gate.get("gate") or "n/a"),
                str(gate.get("arm") or "n/a"),
                str(gate.get("status") or "n/a"),
                str(gate.get("action") or "n/a"),
                _fmt(evidence.get("n"), 0),
                _fmt(evidence.get("paired_n"), 0),
                _fmt(evidence.get("reps_min"), 1),
                _fmt(evidence.get("rec_min")),
                _pct(evidence.get("route")),
                _pct(evidence.get("scheduled_graph_fanout")),
                _pct(evidence.get("top_anchor_audit")),
                _pct(evidence.get("top_anchor_unvalidated")),
                _fmt(evidence.get("generic_min_reads"), 1),
                _fmt(evidence.get("route_fanout_hold_count"), 1),
                ",".join(str(blocker) for blocker in blockers) or "-",
            ]
        )
        + " |"
    )
    lines.append("")
    lines.append(f"## Paired Delta Vs `{BASELINE}`")
    lines.append("")
    head2 = ["category", "arm", "n", "Δrec@5", "Δtokens", "Δturns", "verdict"]
    lines.append("| " + " | ".join(head2) + " |")
    lines.append("| " + " | ".join("---" for _ in head2) + " |")
    for key, row in sorted(agg["paired"].items()):
        cat, arm = key.split("/", 1)
        if row["n"] == 0:
            continue
        drec = row.get("delta_rec5")
        dtok = row.get("delta_tokens")
        lines.append(
            "| "
            + " | ".join(
                [
                    cat,
                    arm,
                    str(row["n"]),
                    _fmt(drec),
                    _fmt(dtok, 0),
                    _fmt(row.get("delta_turns"), 1),
                    _verdict(drec, dtok),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "Iteration gate: keep a change only if the ALL row is not a rec@5 "
        f"REGRESS by more than {EPS:.2f} and tokens/turns move in the intended "
        "direction; then inspect category rows to avoid hiding behavioral "
        "regressions behind easy symbol-hint wins."
    )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cells-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--preflight-route-lifecycle-config", type=Path)
    parser.add_argument("--route-gate-synthesis-configs", default=None)
    parser.add_argument("--route-gate-categories", default=None)
    parser.add_argument("--route-gate-max-queries-per-category", type=int, default=None)
    parser.add_argument(
        "--promotion-profile",
        action="append",
        default=[],
        metavar="PROFILE",
        help=(
            "Apply a named promotion profile that composes the required gates. "
            "Repeat or comma-separate to apply multiple profiles. Available: "
            + ", ".join(sorted(PROMOTION_PROFILES))
        ),
    )
    parser.add_argument(
        "--list-promotion-profiles",
        action="store_true",
        help="Print the available promotion profiles as JSON and exit.",
    )
    parser.add_argument(
        "--require-route-lifecycle-gate",
        action="store_true",
        help=(
            "Return non-zero unless the M3.5bb scheduled route lifecycle gate "
            "passes. The markdown/JSON artifacts are still written first."
        ),
    )
    parser.add_argument(
        "--require-tool-envelope-health",
        action="store_true",
        help=(
            "Return non-zero unless all arms have schema-v2 typed tool envelopes "
            "with full coverage and no typed errors, skips, or truncation. "
            "The markdown/JSON artifacts are still written first."
        ),
    )
    parser.add_argument(
        "--require-feedback-gate",
        action="append",
        default=[],
        metavar="ARM",
        help=(
            "Return non-zero unless ARM is promotable against the grep baseline "
            "in this feedback probe. Repeat or comma-separate to check multiple "
            "arms. The markdown/JSON artifacts are still written first."
        ),
    )
    parser.add_argument(
        "--feedback-gate-require-savings",
        action="store_true",
        help=(
            "Require each feedback-gated arm to reduce tokens or turns in "
            "addition to avoiding recall regressions."
        ),
    )
    parser.add_argument(
        "--require-answer-evidence",
        action="append",
        default=[],
        metavar="ARM",
        help=(
            "Return non-zero unless ARM's committed answer spans are backed by "
            "trace evidence. Repeat or comma-separate to check multiple arms."
        ),
    )
    parser.add_argument(
        "--answer-evidence-min-trace-rate",
        type=float,
        default=1.0,
        help="Minimum per-arm worst-case trace-evidenced answer span ratio.",
    )
    parser.add_argument(
        "--answer-evidence-min-read-rate",
        type=float,
        default=None,
        help=(
            "Optional minimum per-arm worst-case read-confirmed answer span "
            "ratio. Omit to allow graph/LSP trace evidence that was not read."
        ),
    )
    parser.add_argument(
        "--answer-evidence-max-unconfirmed",
        type=float,
        default=0.0,
        help="Maximum per-arm worst-case count of answer spans with no trace evidence.",
    )
    parser.add_argument(
        "--require-exact-replay",
        action="append",
        default=[],
        metavar="ARM",
        help=(
            "Return non-zero unless ARM's cells are event-replayable and exact "
            "message replayable. Repeat or comma-separate to check multiple arms."
        ),
    )
    args = parser.parse_args(argv)

    if args.list_promotion_profiles:
        print(json.dumps(PROMOTION_PROFILES, indent=2))
        return 0

    profile_names = _split_feedback_gate_arms(args.promotion_profile)
    unknown_profiles = [
        name for name in profile_names if name not in PROMOTION_PROFILES
    ]
    if unknown_profiles:
        parser.error(
            "unknown promotion profile(s): "
            + ", ".join(unknown_profiles)
            + "; available: "
            + ", ".join(sorted(PROMOTION_PROFILES))
        )

    if args.preflight_route_lifecycle_config and not args.cells_dir:
        preflight = _route_lifecycle_config_preflight(
            args.preflight_route_lifecycle_config,
            synthesis_configs=args.route_gate_synthesis_configs,
            categories=args.route_gate_categories,
            max_queries_per_category=args.route_gate_max_queries_per_category,
        )
        print(json.dumps(preflight, indent=2))
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / "route_lifecycle_preflight.json").write_text(
                json.dumps(preflight, indent=2),
                encoding="utf-8",
            )
        return 0 if preflight.get("ready") else 2
    if not args.cells_dir:
        parser.error("--cells-dir is required unless preflighting a route config")

    agg = aggregate(_load(args.cells_dir))
    requirements = _promotion_profile_requirements(
        profile_names,
        feedback_arms=_split_feedback_gate_arms(args.require_feedback_gate),
        feedback_require_savings=args.feedback_gate_require_savings,
        answer_evidence_arms=_split_feedback_gate_arms(args.require_answer_evidence),
        replay_arms=_split_feedback_gate_arms(args.require_exact_replay),
        answer_min_trace_rate=args.answer_evidence_min_trace_rate,
        answer_min_read_rate=args.answer_evidence_min_read_rate,
        answer_max_unconfirmed=args.answer_evidence_max_unconfirmed,
        require_route_lifecycle=args.require_route_lifecycle_gate,
        require_tool_envelope_health=args.require_tool_envelope_health,
    )
    feedback_gate_arms = requirements["feedback_arms"]
    if feedback_gate_arms:
        agg["feedback_iteration_gate"] = _feedback_iteration_gate(
            agg,
            feedback_gate_arms,
            require_savings=requirements["feedback_require_savings"],
        )
    answer_evidence_arms = requirements["answer_evidence_arms"]
    if answer_evidence_arms:
        agg["answer_evidence_gate"] = _answer_evidence_gate(
            agg,
            answer_evidence_arms,
            min_trace_rate=requirements["answer_evidence_min_trace_rate"],
            min_read_rate=requirements["answer_evidence_min_read_rate"],
            max_unconfirmed=requirements["answer_evidence_max_unconfirmed"],
        )
    replay_arms = requirements["replay_arms"]
    if replay_arms:
        agg["replay_readiness_gate"] = _replay_readiness_gate(agg, replay_arms)
    if profile_names:
        agg["promotion_profile_gate"] = _promotion_profile_gate(agg, profile_names)
        agg["promotion_decision"] = _promotion_decision(agg, profile_names)
    report = render(agg)
    print(report)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "feedback_probe.md").write_text(report, encoding="utf-8")
        (args.output_dir / "feedback_probe.json").write_text(
            json.dumps(agg, indent=2), encoding="utf-8"
        )
        (args.output_dir / "compact_policy_plan.json").write_text(
            json.dumps(agg.get("compact_policy_plan") or {}, indent=2),
            encoding="utf-8",
        )
        (args.output_dir / "answer_diagnosis_plan.json").write_text(
            json.dumps(agg.get("answer_diagnosis_plan") or {}, indent=2),
            encoding="utf-8",
        )
        (args.output_dir / "tool_envelope_plan.json").write_text(
            json.dumps(agg.get("tool_envelope_plan") or {}, indent=2),
            encoding="utf-8",
        )
        (args.output_dir / "route_lifecycle_gate.json").write_text(
            json.dumps(agg.get("route_lifecycle_gate") or {}, indent=2),
            encoding="utf-8",
        )
        if feedback_gate_arms:
            (args.output_dir / "feedback_iteration_gate.json").write_text(
                json.dumps(agg.get("feedback_iteration_gate") or {}, indent=2),
                encoding="utf-8",
            )
        if answer_evidence_arms:
            (args.output_dir / "answer_evidence_gate.json").write_text(
                json.dumps(agg.get("answer_evidence_gate") or {}, indent=2),
                encoding="utf-8",
            )
        if replay_arms:
            (args.output_dir / "replay_readiness_gate.json").write_text(
                json.dumps(agg.get("replay_readiness_gate") or {}, indent=2),
                encoding="utf-8",
            )
        if profile_names:
            (args.output_dir / "promotion_profile_gate.json").write_text(
                json.dumps(agg.get("promotion_profile_gate") or {}, indent=2),
                encoding="utf-8",
            )
            (args.output_dir / "promotion_decision.json").write_text(
                json.dumps(agg.get("promotion_decision") or {}, indent=2),
                encoding="utf-8",
            )
    failure_messages = []
    if requirements["require_route_lifecycle_gate"]:
        gate = agg.get("route_lifecycle_gate") or {}
        if not gate.get("promote"):
            failure_messages.append(
                "route lifecycle gate failed: "
                + str(gate.get("reason") or gate.get("status") or "unknown")
            )
    if requirements["require_tool_envelope_health"]:
        blockers = _tool_envelope_gate_blockers(agg)
        if blockers:
            failure_messages.append(
                "tool envelope health gate failed: " + ", ".join(blockers)
            )
    if feedback_gate_arms:
        gate = agg.get("feedback_iteration_gate") or {}
        if not gate.get("promote"):
            failure_messages.append(
                "feedback iteration gate failed: "
                + ", ".join(str(blocker) for blocker in gate.get("blockers") or [])
            )
    if answer_evidence_arms:
        gate = agg.get("answer_evidence_gate") or {}
        if not gate.get("promote"):
            failure_messages.append(
                "answer evidence gate failed: "
                + ", ".join(str(blocker) for blocker in gate.get("blockers") or [])
            )
    if replay_arms:
        gate = agg.get("replay_readiness_gate") or {}
        if not gate.get("promote"):
            failure_messages.append(
                "replay readiness gate failed: "
                + ", ".join(str(blocker) for blocker in gate.get("blockers") or [])
            )
    if profile_names:
        gate = agg.get("promotion_profile_gate") or {}
        if not gate.get("promote"):
            failure_messages.append(
                "promotion profile gate failed: "
                + ", ".join(str(blocker) for blocker in gate.get("blockers") or [])
            )
    if failure_messages:
        print("; ".join(failure_messages), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
