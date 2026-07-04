# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for trace-aware feedback probe aggregation."""

from __future__ import annotations

import json
import subprocess
import sys

from scripts.agent_compile import aggregate_feedback_probe as probe


def _cell(
    query_id: str,
    arm: str,
    rec: float,
    tokens: int,
    *,
    compact=False,
    fallback=False,
    graph_expansion=False,
    scheduled_context=False,
    scheduled_operation=None,
    scheduled_route_budget_applied=False,
    scheduled_route_budget_dropped=0,
    scheduled_route_budget_reason=None,
    scheduled_route_budget_missing_required_count=0,
    scheduled_route_read_calls=None,
    scheduled_route_generic_min_reads=None,
    scheduled_route_bridge_query=None,
    scheduled_route_read_confirmed_count=0,
    scheduled_route_read_confirmed_core_count=0,
    scheduled_route_read_confirmed_protected_count=0,
    scheduled_route_unread_core_count=0,
    scheduled_route_unread_protected_count=0,
    scheduled_route_fanout_hold_count=None,
    scheduled_route_fanout_hold_first_turn=None,
    scheduled_route_fanout_hold_read_calls=None,
    scheduled_route_fanout_hold_search_calls=None,
    scheduled_route_fanout_hold_generic_min_reads=None,
    scheduled_final_guard=False,
    scheduled_anchor_audit=False,
    scheduled_top_anchor_audit=False,
    scheduled_top_anchor_audit_missing_unread=0,
    scheduled_top_anchor_audit_answer_only=False,
    scheduled_top_anchor_audit_bounded_tool_loop=False,
    scheduled_top_anchor_audit_deterministic_patch=False,
    scheduled_top_anchor_audit_read_only=False,
    scheduled_top_anchor_audit_cascade_fallback=False,
    scheduled_ordering_audit=False,
    scheduled_skipped=False,
    lsp=False,
    lsp_source="lsp_definition",
    lsp_route_tool_turn=None,
    lsp_route_tool_results=None,
    verified_preload=True,
    file_reads=None,
    answer_span_evidence=None,
    answer_diagnosis=None,
    answer_path_alias_normalized=None,
    answer_path_alias_replacements=None,
    answer_schema_salvaged=None,
    answer_schema_salvage_locations=None,
    answer_location_order_normalized=None,
    answer_location_order_promotions=None,
    turns=2,
    compact_pruned_chars=1200,
    compact_seed_chars=800,
    compact_tail_chars=200,
    compact_kept_reads=0,
):
    events = [{"kind": "run_start", "turn": 0, "data": {}}]
    if compact:
        events.append({"kind": "compaction", "turn": 1, "data": {}})
    if fallback:
        events.append(
            {
                "kind": "fallback",
                "turn": 1,
                "data": {"strategy": "preload_unverified"},
            }
        )
    if graph_expansion:
        events.append(
            {
                "kind": "graph_expansion",
                "turn": 2,
                "data": {
                    "reason": "search_calls",
                    "seed_count": 1,
                    "node_count": 3,
                },
            }
        )
    if scheduled_context:
        data = {
            "source": "scheduled_graph_lsp",
            "reason": "search_calls",
            "seed_count": 1,
            "node_count": 2,
        }
        if scheduled_operation is not None:
            data["operation"] = scheduled_operation
        if scheduled_route_read_calls is not None:
            data["read_calls"] = scheduled_route_read_calls
        if scheduled_route_generic_min_reads is not None:
            data["generic_min_read_calls"] = scheduled_route_generic_min_reads
        if scheduled_route_bridge_query is not None:
            data["bridge_route_query"] = scheduled_route_bridge_query
        if (
            scheduled_route_budget_applied
            or scheduled_route_budget_dropped
            or scheduled_route_budget_reason
            or scheduled_route_budget_missing_required_count
        ):
            data["route_budget_applied"] = scheduled_route_budget_applied
            data["route_budget_dropped"] = scheduled_route_budget_dropped
            if scheduled_route_budget_reason:
                data["route_budget_reason"] = scheduled_route_budget_reason
            if scheduled_route_budget_missing_required_count:
                data["route_budget_missing_required_count"] = (
                    scheduled_route_budget_missing_required_count
                )
        if (
            scheduled_route_read_confirmed_count
            or scheduled_route_read_confirmed_core_count
            or scheduled_route_read_confirmed_protected_count
            or scheduled_route_unread_core_count
            or scheduled_route_unread_protected_count
        ):
            data["route_read_confirmed_count"] = scheduled_route_read_confirmed_count
            data["route_read_confirmed_core_count"] = (
                scheduled_route_read_confirmed_core_count
            )
            data["route_read_confirmed_protected_count"] = (
                scheduled_route_read_confirmed_protected_count
            )
            data["route_unread_core_count"] = scheduled_route_unread_core_count
            data["route_unread_protected_count"] = (
                scheduled_route_unread_protected_count
            )
        events.append(
            {
                "kind": "scheduled_context",
                "turn": 2,
                "data": data,
            }
        )
    if scheduled_final_guard:
        events.append(
            {
                "kind": "scheduled_context",
                "turn": 4,
                "data": {
                    "source": "scheduled_graph_lsp",
                    "reason": "route_finalization_guard",
                    "operation": "lsp_route_finalization_guard",
                    "node_count": 3,
                    "priority_count": 2,
                },
            }
        )
    if scheduled_anchor_audit:
        events.append(
            {
                "kind": "scheduled_anchor_audit",
                "turn": 3,
                "data": {
                    "strategy": "read_symbol_definition_correction",
                    "offered_count": 3,
                    "read_count": 2,
                    "cited_count": 0,
                    "correction_anchors": 2,
                },
            }
        )
    if scheduled_top_anchor_audit:
        correction_mode = "tool_loop"
        if scheduled_top_anchor_audit_answer_only:
            correction_mode = "answer_only"
        elif scheduled_top_anchor_audit_bounded_tool_loop:
            correction_mode = "bounded_tool_loop"
        elif scheduled_top_anchor_audit_deterministic_patch:
            correction_mode = "deterministic_answer_patch"
        elif scheduled_top_anchor_audit_read_only:
            correction_mode = "read_only_tool_loop"
        attempted_correction_mode = correction_mode
        if scheduled_top_anchor_audit_cascade_fallback:
            correction_mode = f"{correction_mode}_fallback"
        events.append(
            {
                "kind": "scheduled_top_anchor_audit",
                "turn": 4,
                "data": {
                    "strategy": "priority_anchor_top_k_correction",
                    "correction_mode": correction_mode,
                    "attempted_correction_mode": attempted_correction_mode,
                    "cascade_fallback": scheduled_top_anchor_audit_cascade_fallback,
                    "validation_ok": not scheduled_top_anchor_audit_cascade_fallback,
                    "validation_missing_count": (
                        1 if scheduled_top_anchor_audit_cascade_fallback else 0
                    ),
                    "missing_count": 1,
                    "missing_unread_count": scheduled_top_anchor_audit_missing_unread,
                    "read_count": 4,
                    "answer_span_count": 7,
                    "top_k": 5,
                },
            }
        )
    if scheduled_ordering_audit:
        events.append(
            {
                "kind": "scheduled_answer_ordering_audit",
                "turn": 5,
                "data": {
                    "strategy": "answer_ordering_correction",
                    "reason": "helper_crowding",
                    "answer_span_count": 8,
                    "top_k": 5,
                },
            }
        )
    events.append({"kind": "run_end", "turn": 2, "data": {}})
    context = [
        {
            "source": "preload",
            "state": "verified" if verified_preload else "offered",
            "path": "src/a.py",
            "metadata": {"rank": 1, "read_confirmed": verified_preload},
        },
        {
            "source": "preload",
            "state": "offered",
            "path": "src/b.py",
            "metadata": {"rank": 2, "read_confirmed": False},
        },
        {
            "source": "read",
            "state": "read",
            "path": "src/a.py",
            "metadata": {"result_chars": 100},
        },
        {
            "source": "grep",
            "state": "offered",
            "metadata": {"result_chars": 40},
        },
    ]
    if lsp:
        context.append(
            {
                "source": lsp_source,
                "state": "offered",
                "path": "src/a.py",
                "metadata": {"result_chars": 80},
            }
        )
    if lsp_route_tool_turn is not None:
        events.append(
            {
                "kind": "tool_call",
                "turn": lsp_route_tool_turn,
                "data": {
                    "tool_call_id": "call_lsp_route",
                    "tool": "lsp_route",
                    "arguments": {"symbols": ["Foo", "Bar"]},
                    "error": None,
                },
            }
        )
    if scheduled_context:
        context.append(
            {
                "source": "scheduled_graph_lsp",
                "state": "offered",
                "metadata": {"result_chars": 120, "node_count": 2},
            }
        )
    if compact:
        context.append(
            {
                "source": "compaction",
                "state": "summarized",
                "metadata": {
                    "summary_reads": 1,
                    "pruned_tool_result_chars": compact_pruned_chars,
                    "seed_chars": compact_seed_chars,
                    "protected_tail_chars": compact_tail_chars,
                    "kept_read_outputs": compact_kept_reads,
                },
            }
        )
    return {
        "success": True,
        "query_id": query_id,
        "category": "behavioral",
        "subset_id": arm,
        "format_failed": False,
        "answer_diagnosis": answer_diagnosis,
        "answer_path_alias_normalized": answer_path_alias_normalized,
        "answer_path_alias_replacements": answer_path_alias_replacements,
        "answer_schema_salvaged": answer_schema_salvaged,
        "answer_schema_salvage_locations": answer_schema_salvage_locations,
        "answer_location_order_normalized": answer_location_order_normalized,
        "answer_location_order_promotions": answer_location_order_promotions,
        "scheduled_route_fanout_hold_count": scheduled_route_fanout_hold_count,
        "scheduled_route_fanout_hold_first_turn": (
            scheduled_route_fanout_hold_first_turn
        ),
        "scheduled_route_fanout_hold_read_calls": scheduled_route_fanout_hold_read_calls,
        "scheduled_route_fanout_hold_search_calls": (
            scheduled_route_fanout_hold_search_calls
        ),
        "scheduled_route_fanout_hold_generic_min_reads": (
            scheduled_route_fanout_hold_generic_min_reads
        ),
        "metrics": {
            "answer_blocks": {"5": {"recall": rec}},
            "files": {"5": {"recall": rec}},
        },
        "total_tokens": tokens,
        "total_turns": turns,
        "trace": {
            "events": events,
            "context": context,
        },
        "scheduled_context_triggered": scheduled_context,
        "scheduled_context_operation": scheduled_operation,
        "scheduled_context_nodes": 2 if scheduled_context else 0,
        "scheduled_context_skipped": scheduled_skipped,
        "scheduled_context_skip_reason": (
            "preload_verified" if scheduled_skipped else None
        ),
        "scheduled_anchor_audit_triggered": scheduled_anchor_audit,
        "scheduled_anchor_audit_offered": 3 if scheduled_anchor_audit else 0,
        "scheduled_anchor_audit_read": 2 if scheduled_anchor_audit else 0,
        "scheduled_anchor_audit_cited": 0,
        "scheduled_top_anchor_audit_triggered": scheduled_top_anchor_audit,
        "scheduled_top_anchor_audit_missing": (1 if scheduled_top_anchor_audit else 0),
        "scheduled_top_anchor_audit_missing_unread": (
            scheduled_top_anchor_audit_missing_unread
        ),
        "scheduled_ordering_audit_triggered": scheduled_ordering_audit,
        "scheduled_ordering_audit_span_count": (8 if scheduled_ordering_audit else 0),
        "file_reads": file_reads or [],
        "answer_span_evidence": answer_span_evidence or [],
        "tool_calls": (
            [
                {
                    "skill_id": "lsp_route",
                    "n_results": lsp_route_tool_results,
                    "error": False,
                }
            ]
            if lsp_route_tool_turn is not None
            else []
        ),
    }


def _report_row(report: str, arm: str) -> dict[str, str]:
    lines = report.splitlines()
    header_line = next(line for line in lines if line.startswith("| arm |"))
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(line for line in lines if line.startswith(f"| {arm} |"))
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _policy_row(report: str, arm: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Compact Policy Plan")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| arm |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(
        line for line in lines[heading_index:] if line.startswith(f"| {arm} |")
    )
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _answer_plan_row(report: str, arm: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Answer Diagnosis Plan")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| arm |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(
        line for line in lines[heading_index:] if line.startswith(f"| {arm} |")
    )
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _tool_plan_row(report: str, arm: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Tool Envelope Plan")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| arm |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(
        line for line in lines[heading_index:] if line.startswith(f"| {arm} |")
    )
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _route_gate_row(report: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Route Lifecycle Gate")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| gate |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(
        line
        for line in lines[heading_index:]
        if line.startswith("| M3.5bb_route_lifecycle_repeat |")
    )
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _feedback_gate_row(report: str, arm: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Feedback Iteration Gate")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| gate |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(line for line in lines[heading_index:] if f"| {arm} |" in line)
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _answer_evidence_gate_row(report: str, arm: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Answer Evidence Gate")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| gate |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(line for line in lines[heading_index:] if f"| {arm} |" in line)
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _replay_gate_row(report: str, arm: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Replay Readiness Gate")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| gate |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(line for line in lines[heading_index:] if f"| {arm} |" in line)
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _promotion_profile_gate_row(report: str, profile: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Promotion Profile Gate")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| gate |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(line for line in lines[heading_index:] if f"| {profile} |" in line)
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _promotion_decision_row(report: str) -> dict[str, str]:
    lines = report.splitlines()
    heading_index = lines.index("## Promotion Decision")
    header_line = next(
        line for line in lines[heading_index:] if line.startswith("| decision |")
    )
    headers = [col.strip() for col in header_line.strip("|").split("|")]
    row_line = next(
        line
        for line in lines[heading_index:]
        if line.startswith("| promote |") or line.startswith("| revise |")
    )
    values = [col.strip() for col in row_line.strip("|").split("|")]
    return dict(zip(headers, values, strict=False))


def _route_gate_pass_cells(*, deterministic_top_anchor_patch=False):
    cells = []
    for query_id in ["caddy_bridge", "caddy_traceback", "ruff_bridge", "ruff_trace"]:
        cells.append(_cell(query_id, "grep_only", 0.5, 1000))
        for _rep in range(2):
            cells.append(
                _cell(
                    query_id,
                    "preinj_graph_scheduled",
                    1.0,
                    700,
                    scheduled_context=True,
                    scheduled_operation="lsp_route",
                    scheduled_route_read_calls=4,
                    scheduled_route_generic_min_reads=4,
                    scheduled_route_bridge_query=True,
                    scheduled_route_fanout_hold_count=1,
                    scheduled_route_fanout_hold_first_turn=2,
                    scheduled_route_fanout_hold_read_calls=1,
                    scheduled_route_fanout_hold_search_calls=2,
                    scheduled_route_fanout_hold_generic_min_reads=4,
                    scheduled_top_anchor_audit=deterministic_top_anchor_patch,
                    scheduled_top_anchor_audit_deterministic_patch=(
                        deterministic_top_anchor_patch
                    ),
                )
            )
    return cells


def _write_cells(cells_dir, cells):
    cells_dir.mkdir()
    for index, cell in enumerate(cells):
        (cells_dir / f"cell_{index}.json").write_text(
            json.dumps(cell),
            encoding="utf-8",
        )


def _with_exact_replay(cell):
    cell["messages"] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "query"},
        {"role": "assistant", "content": "answer"},
    ]
    return cell


def test_feedback_probe_uses_trace_and_paired_deltas():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell("q1", "preinj_eager_compact", 0.5, 700, compact=True),
    ]

    agg = probe.aggregate(cells)

    compact = agg["by_arm"]["preinj_eager_compact"]
    assert compact["compacted"] == 1.0
    assert compact["reads"] == 1.0
    assert compact["offered"] == 1.0
    assert compact["preload"] == 2.0
    assert compact["preload_verified"] == 1.0
    assert compact["preload_verify_rate"] == 0.5
    assert compact["fallback"] == 0.0
    assert compact["graph_expansion"] == 0.0
    assert compact["lsp"] == 0.0
    assert compact["lsp_used"] == 0.0
    assert compact["summarized"] == 1.0
    assert compact["pruned_tool_chars"] == 1200.0
    assert compact["seed_chars"] == 800.0
    assert compact["tail_chars"] == 200.0
    assert compact["compact_keep_reads"] == 0.0
    assert compact["compact_prune_ratio"] == 0.6
    assert compact["compact_tail_share"] == 0.25
    assert compact["has_trace"] == 1.0
    assert agg["compact_policy"]["preinj_eager_compact"] == "hold"
    assert agg["compact_policy_plan"]["preinj_eager_compact"]["action"] == (
        "keep_current_compact_policy"
    )
    assert agg["compact_policy_plan"]["preinj_eager_compact"]["preload_patch"] == {}

    paired = agg["paired"]["ALL/preinj_eager_compact"]
    assert paired["n"] == 1
    assert paired["delta_rec5"] == 0.0
    assert paired["delta_tokens"] == -300

    report = probe.render(agg)
    assert "SAVE" in report
    assert "comp%" in report
    assert "summary" in report
    assert "pruned_k" in report
    assert "cprune%" in report
    assert "cpol" in report
    assert "rep_n" in report
    assert "rec_min" in report
    assert "diag" in report
    assert "tok_hi" in report
    assert "tok_rng" in report
    assert "turn_hi" in report
    assert "turn_rng" in report
    assert "rrep" in report
    assert "rovlp%" in report
    assert "rovlp_l" in report
    assert "preload" in report
    assert "pre_vfy%" in report
    assert "te%" in report
    assert "rc%" in report
    assert "fb%" in report
    assert "gexp%" in report
    assert "lsp%" in report
    assert "sched%" in report
    assert "route%" in report
    assert "rb%" in report
    assert "rb_drop" in report
    assert "sdef%" in report
    assert "fg%" in report
    assert "sfan%" in report
    assert "op?%" in report
    assert "aud%" in report
    assert "sskip%" in report
    assert "taud%" in report
    assert "taud_bt%" in report
    assert "taud_dp%" in report
    assert "taud_ro%" in report
    assert "taud_fb%" in report
    assert "ord%" in report
    row = _report_row(report, "preinj_eager_compact")
    assert row["cprune%"] == "60%"
    assert row["tail%"] == "25%"
    assert row["keep"] == "0.0"
    assert row["cpol"] == "hold"
    policy_row = _policy_row(report, "preinj_eager_compact")
    assert policy_row["policy"] == "hold"
    assert policy_row["patch"] == "-"


def test_feedback_probe_recommends_keep_read_for_compact_reread_tax():
    cells = [
        _cell("q1", "grep_only", 1.0, 1000),
        _cell(
            "q1",
            "preinj_eager_compact",
            1.0,
            800,
            compact=True,
            file_reads=[
                {"file_path": "src/a.py", "offset": 10, "limit": 5, "turn": 1},
                {"file_path": "src/a.py", "offset": 10, "limit": 5, "turn": 2},
            ],
            compact_kept_reads=0,
        ),
    ]

    agg = probe.aggregate(cells)
    report = probe.render(agg)
    row = _report_row(report, "preinj_eager_compact")

    assert agg["by_arm"]["preinj_eager_compact"]["read_repeat_offsets"] == 1.0
    assert agg["compact_policy"]["preinj_eager_compact"] == "try_keep1"
    assert agg["compact_policy_plan"]["preinj_eager_compact"]["preload_patch"] == {
        "compact_keep_reads": 1
    }
    assert row["cpol"] == "try_keep1"
    assert _policy_row(report, "preinj_eager_compact")["patch"] == (
        "compact_keep_reads=1"
    )


def test_feedback_probe_recommends_tail_trim_when_tail_dominates_seed():
    cells = [
        _cell("q1", "grep_only", 1.0, 1000),
        _cell(
            "q1",
            "preinj_eager_compact",
            1.0,
            800,
            compact=True,
            compact_seed_chars=1000,
            compact_tail_chars=500,
            compact_pruned_chars=1600,
            compact_kept_reads=1,
        ),
    ]

    agg = probe.aggregate(cells)

    assert agg["by_arm"]["preinj_eager_compact"]["compact_tail_share"] == 0.5
    assert agg["compact_policy"]["preinj_eager_compact"] == "trim_tail"
    assert agg["compact_policy_plan"]["preinj_eager_compact"]["preload_patch"] == {
        "compact_tail_chars": 250
    }


def test_feedback_probe_writes_compact_policy_plan_json(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    cells_dir.mkdir()
    output_dir = tmp_path / "out"
    (cells_dir / "grep.json").write_text(
        json.dumps(_cell("q1", "grep_only", 1.0, 1000)),
        encoding="utf-8",
    )
    (cells_dir / "compact.json").write_text(
        json.dumps(
            _cell(
                "q1",
                "preinj_eager_compact",
                1.0,
                800,
                compact=True,
                compact_seed_chars=1000,
                compact_tail_chars=500,
                compact_pruned_chars=1600,
                compact_kept_reads=1,
            )
        ),
        encoding="utf-8",
    )

    assert (
        probe.main(["--cells-dir", str(cells_dir), "--output-dir", str(output_dir)])
        == 0
    )
    capsys.readouterr()

    plan = json.loads((output_dir / "compact_policy_plan.json").read_text())
    answer_plan = json.loads((output_dir / "answer_diagnosis_plan.json").read_text())
    tool_plan = json.loads((output_dir / "tool_envelope_plan.json").read_text())
    route_gate = json.loads((output_dir / "route_lifecycle_gate.json").read_text())
    assert plan["preinj_eager_compact"]["policy"] == "trim_tail"
    assert plan["preinj_eager_compact"]["preload_patch"] == {"compact_tail_chars": 250}
    assert answer_plan["preinj_eager_compact"]["action"] == "keep_answer_contract"
    assert tool_plan["preinj_eager_compact"]["status"] == "missing"
    assert route_gate["status"] == "missing"
    assert route_gate["blockers"] == ["missing_preinj_graph_scheduled_arm"]


def test_feedback_probe_reports_answer_diagnosis_mix():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000, answer_diagnosis="rank_gap"),
        _cell("q2", "grep_only", 0.0, 1000, answer_diagnosis="content_gap"),
        _cell("q1", "preinj_graph_scheduled", 1.0, 900, answer_diagnosis="ok"),
    ]

    agg = probe.aggregate(cells)
    baseline = agg["by_arm"]["grep_only"]
    scheduled = agg["by_arm"]["preinj_graph_scheduled"]
    report = probe.render(agg)

    assert baseline["answer_diag_rank_gap"] == 0.5
    assert baseline["answer_diag_content_gap"] == 0.5
    assert scheduled["answer_diag_ok"] == 1.0
    assert _report_row(report, "grep_only")["diag"] == "rank:50%,content:50%"
    assert _report_row(report, "preinj_graph_scheduled")["diag"] == "ok:100%"
    assert agg["answer_diagnosis_plan"]["grep_only"]["action"] == (
        "tighten_answer_ordering"
    )
    assert _answer_plan_row(report, "grep_only")["dominant"] == "rank_gap"
    assert _answer_plan_row(report, "grep_only")["action"] == (
        "tighten_answer_ordering"
    )


def test_feedback_probe_reports_strict_vs_all_answer_recall():
    rank_gap = _cell("q1", "grep_only", 0.5, 1000, answer_diagnosis="rank_gap")
    rank_gap["answer_all_metrics"] = {"recall": 1.0}
    rank_gap["answer_path_alias_metrics"] = {"recall": 1.0}
    alias_gap = _cell("q2", "grep_only", 0.0, 900, answer_diagnosis="path_alias_gap")
    alias_gap["answer_all_metrics"] = {"recall": 0.0}
    alias_gap["answer_path_alias_metrics"] = {"recall": 1.0}

    agg = probe.aggregate([rank_gap, alias_gap])
    row = agg["by_arm"]["grep_only"]
    answer_row = _answer_plan_row(probe.render(agg), "grep_only")

    assert row["rec5"] == 0.25
    assert row["answer_rec_all"] == 0.5
    assert row["answer_path_alias_rec_all"] == 1.0
    assert agg["answer_diagnosis_plan"]["grep_only"]["answer_rec_all"] == 0.5
    assert answer_row["rec@5"] == "0.250"
    assert answer_row["rec@all"] == "0.500"
    assert answer_row["alias@all"] == "1.000"


def test_feedback_probe_passes_route_lifecycle_gate():
    cells = _route_gate_pass_cells()

    agg = probe.aggregate(cells)
    gate = agg["route_lifecycle_gate"]
    row = _route_gate_row(probe.render(agg))

    assert gate["status"] == "pass"
    assert gate["promote"] is True
    assert gate["action"] == "promote_route_lifecycle_slice"
    assert gate["blockers"] == []
    assert gate["evidence"]["route"] == 1.0
    assert gate["evidence"]["scheduled_graph_fanout"] == 0.0
    assert gate["evidence"]["n"] == 4
    assert gate["evidence"]["paired_n"] == 4
    assert gate["evidence"]["reps_min"] == 2.0
    assert row["status"] == "pass"
    assert row["n"] == "4"
    assert row["paired_n"] == "4"
    assert row["rep_min"] == "2.0"
    assert row["rec_min"] == "1.000"
    assert row["route%"] == "100%"
    assert row["sfan%"] == "0%"
    assert row["taud%"] == "0%"
    assert row["taud_bad%"] == "0%"
    assert row["rgmin"] == "4.0"
    assert row["rhold"] == "1.0"
    assert row["blockers"] == "-"


def test_feedback_probe_route_lifecycle_gate_allows_validated_patch():
    cells = _route_gate_pass_cells(deterministic_top_anchor_patch=True)

    agg = probe.aggregate(cells)
    gate = agg["route_lifecycle_gate"]
    report = probe.render(agg)
    arm_row = _report_row(report, "preinj_graph_scheduled")
    gate_row = _route_gate_row(report)

    assert gate["status"] == "pass"
    assert gate["promote"] is True
    assert gate["blockers"] == []
    assert gate["evidence"]["top_anchor_audit"] == 1.0
    assert gate["evidence"]["top_anchor_validated_deterministic"] == 1.0
    assert gate["evidence"]["top_anchor_unvalidated"] == 0.0
    assert arm_row["taud%"] == "100%"
    assert arm_row["taud_dp%"] == "100%"
    assert arm_row["taud_vdp%"] == "100%"
    assert arm_row["taud_bad%"] == "0%"
    assert gate_row["taud%"] == "100%"
    assert gate_row["taud_bad%"] == "0%"


def test_feedback_probe_fails_route_lifecycle_gate_with_blockers():
    cells = [
        _cell("q1", "grep_only", 1.0, 1000),
        _cell("q2", "grep_only", 1.0, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            0.5,
            1200,
            scheduled_context=True,
            scheduled_operation="lsp_route",
            scheduled_route_generic_min_reads=3,
            scheduled_top_anchor_audit=True,
        ),
        _cell(
            "q2",
            "preinj_graph_scheduled",
            1.0,
            900,
            scheduled_context=True,
            scheduled_operation="graph_fanout",
        ),
    ]

    agg = probe.aggregate(cells)
    gate = agg["route_lifecycle_gate"]
    row = _route_gate_row(probe.render(agg))

    assert gate["status"] == "fail"
    assert gate["promote"] is False
    assert gate["action"] == "revise_route_lifecycle_policy"
    assert gate["blockers"] == [
        "insufficient_query_coverage",
        "insufficient_paired_baseline_coverage",
        "insufficient_repeat_coverage",
        "rec_min_below_1.0",
        "lsp_route_not_all_cells",
        "search_fanout_preempted_route",
        "top_anchor_unvalidated_correction",
        "generic_route_threshold_below_4",
    ]
    assert gate["incomplete_blockers"] == [
        "insufficient_query_coverage",
        "insufficient_paired_baseline_coverage",
        "insufficient_repeat_coverage",
    ]
    assert gate["failure_blockers"] == [
        "rec_min_below_1.0",
        "lsp_route_not_all_cells",
        "search_fanout_preempted_route",
        "top_anchor_unvalidated_correction",
        "generic_route_threshold_below_4",
    ]
    assert row["status"] == "fail"
    assert row["route%"] == "50%"
    assert row["sfan%"] == "50%"
    assert row["taud%"] == "50%"
    assert row["taud_bad%"] == "50%"
    assert "rec_min_below_1.0" in row["blockers"]


def test_feedback_probe_marks_route_lifecycle_gate_incomplete_for_stale_artifact():
    cells = []
    for query_id in ["caddy_bridge", "caddy_traceback", "ruff_bridge", "ruff_trace"]:
        cells.append(_cell(query_id, "grep_only", 0.5, 1000))
        for _rep in range(2):
            cells.append(
                _cell(
                    query_id,
                    "preinj_graph_scheduled",
                    1.0,
                    700,
                    scheduled_context=True,
                    scheduled_operation="lsp_route",
                    scheduled_route_read_calls=4,
                    scheduled_route_bridge_query=True,
                )
            )

    agg = probe.aggregate(cells)
    gate = agg["route_lifecycle_gate"]
    row = _route_gate_row(probe.render(agg))

    assert gate["status"] == "incomplete"
    assert gate["promote"] is False
    assert gate["action"] == "rerun_route_scheduler_repeat_gate"
    assert gate["incomplete_blockers"] == ["missing_generic_min_reads"]
    assert gate["failure_blockers"] == []
    assert gate["blockers"] == ["missing_generic_min_reads"]
    assert row["status"] == "incomplete"
    assert row["blockers"] == "missing_generic_min_reads"


def test_feedback_probe_require_route_lifecycle_gate_fails(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell("q1", "grep_only", 1.0, 1000),
            _cell("q1", "preinj_eager_compact", 1.0, 800, compact=True),
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-route-lifecycle-gate",
        ]
    )
    captured = capsys.readouterr()
    route_gate = json.loads((output_dir / "route_lifecycle_gate.json").read_text())

    assert rc == 2
    assert "route lifecycle gate failed" in captured.err
    assert route_gate["status"] == "missing"
    assert route_gate["promote"] is False


def test_feedback_probe_require_route_lifecycle_gate_passes(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(cells_dir, _route_gate_pass_cells())

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-route-lifecycle-gate",
        ]
    )
    captured = capsys.readouterr()
    route_gate = json.loads((output_dir / "route_lifecycle_gate.json").read_text())

    assert rc == 0
    assert captured.err == ""
    assert route_gate["status"] == "pass"
    assert route_gate["promote"] is True


def test_feedback_probe_feedback_gate_passes_and_writes_artifact(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell("q1", "grep_only", 1.0, 1000, turns=4),
            _cell("q1", "preinj_eager_compact", 1.0, 800, compact=True, turns=3),
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-feedback-gate",
            "preinj_eager_compact",
            "--feedback-gate-require-savings",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "feedback_iteration_gate.json").read_text())
    row = _feedback_gate_row(
        (output_dir / "feedback_probe.md").read_text(),
        "preinj_eager_compact",
    )

    assert rc == 0
    assert captured.err == ""
    assert gate["status"] == "pass"
    assert gate["promote"] is True
    assert gate["criteria"]["require_savings"] is True
    assert gate["runs"][0]["blockers"] == []
    assert row["status"] == "pass"
    assert row["Δrec@5"] == "0.000"
    assert row["Δtokens"] == "-200"
    assert row["Δturns"] == "-1.0"
    assert row["blockers"] == "-"


def test_feedback_probe_feedback_gate_fails_rec_regression(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell("q1", "grep_only", 1.0, 1000),
            _cell("q1", "preinj_eager_compact", 0.5, 800, compact=True),
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-feedback-gate",
            "preinj_eager_compact",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "feedback_iteration_gate.json").read_text())

    assert rc == 2
    assert "feedback iteration gate failed" in captured.err
    assert "preinj_eager_compact:rec5_regression" in captured.err
    assert gate["status"] == "fail"
    assert gate["promote"] is False
    assert "preinj_eager_compact:rec5_regression" in gate["blockers"]


def test_feedback_probe_feedback_gate_requires_savings(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell("q1", "grep_only", 1.0, 1000, turns=2),
            _cell("q1", "preinj_eager_compact", 1.0, 1200, compact=True, turns=2),
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-feedback-gate",
            "preinj_eager_compact",
            "--feedback-gate-require-savings",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "feedback_iteration_gate.json").read_text())

    assert rc == 2
    assert "preinj_eager_compact:no_token_or_turn_savings" in captured.err
    assert gate["blockers"] == ["preinj_eager_compact:no_token_or_turn_savings"]


def test_feedback_probe_feedback_gate_blocks_category_regression(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    baseline_behavioral = _cell("q1", "grep_only", 1.0, 1000)
    compact_behavioral = _cell("q1", "preinj_eager_compact", 0.0, 800, compact=True)
    baseline_traversal = _cell("q2", "grep_only", 0.0, 1000)
    baseline_traversal["category"] = "traversal"
    compact_traversal = _cell("q2", "preinj_eager_compact", 1.0, 800, compact=True)
    compact_traversal["category"] = "traversal"
    _write_cells(
        cells_dir,
        [
            baseline_behavioral,
            compact_behavioral,
            baseline_traversal,
            compact_traversal,
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-feedback-gate",
            "preinj_eager_compact",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "feedback_iteration_gate.json").read_text())

    assert rc == 2
    assert "preinj_eager_compact:behavioral:rec5_regression" in captured.err
    assert gate["runs"][0]["evidence"]["delta_rec5"] == 0.0
    assert gate["blockers"] == ["preinj_eager_compact:behavioral:rec5_regression"]


def test_feedback_probe_preflights_route_lifecycle_config(tmp_path, capsys):
    output_dir = tmp_path / "out"

    rc = probe.main(
        [
            "--preflight-route-lifecycle-config",
            "scripts/agent_compile/configs/haiku_route_scheduler_probe.yaml",
            "--route-gate-synthesis-configs",
            "Go,Rust",
            "--route-gate-categories",
            "traversal",
            "--route-gate-max-queries-per-category",
            "2",
            "--output-dir",
            str(output_dir),
        ]
    )
    captured = capsys.readouterr()
    preflight = json.loads((output_dir / "route_lifecycle_preflight.json").read_text())

    assert rc == 0
    assert '"status": "pass"' in captured.out
    assert preflight["status"] == "pass"
    assert preflight["ready"] is True
    assert preflight["blockers"] == []
    assert preflight["evidence"]["graph_generic_read_symbol_min_reads"] == 4


def test_feedback_probe_preflight_rejects_scheduled_only_config(capsys):
    rc = probe.main(
        [
            "--preflight-route-lifecycle-config",
            "scripts/agent_compile/configs/haiku_route_scheduler_scheduled_only.yaml",
            "--route-gate-synthesis-configs",
            "Go,Rust",
            "--route-gate-categories",
            "traversal",
            "--route-gate-max-queries-per-category",
            "2",
        ]
    )
    captured = capsys.readouterr()
    preflight = json.loads(captured.out)

    assert rc == 2
    assert preflight["status"] == "incomplete"
    assert preflight["ready"] is False
    assert preflight["blockers"] == [
        "missing_grep_only_arm",
        "insufficient_repeat_config",
    ]


def test_feedback_probe_preflight_cli_avoids_retrieval_imports():
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "importtime",
            "scripts/agent_compile/aggregate_feedback_probe.py",
            "--preflight-route-lifecycle-config",
            "scripts/agent_compile/configs/haiku_route_scheduler_probe.yaml",
            "--route-gate-synthesis-configs",
            "Go,Rust",
            "--route-gate-categories",
            "traversal",
            "--route-gate-max-queries-per-category",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "codeminer.eval.retrieval_eval" not in result.stderr
    assert "faiss.loader" not in result.stderr


def test_feedback_probe_reports_answer_path_alias_normalization():
    cells = [
        _cell(
            "q1",
            "grep_only",
            1.0,
            1000,
            answer_path_alias_normalized=True,
            answer_path_alias_replacements=2,
            answer_schema_salvaged=True,
            answer_schema_salvage_locations=3,
            answer_location_order_normalized=True,
            answer_location_order_promotions=1,
        ),
        _cell(
            "q2",
            "grep_only",
            1.0,
            900,
            answer_path_alias_normalized=False,
            answer_schema_salvaged=False,
            answer_location_order_normalized=False,
        ),
    ]

    agg = probe.aggregate(cells)
    row = agg["by_arm"]["grep_only"]
    report_row = _report_row(probe.render(agg), "grep_only")

    assert row["answer_path_alias_normalized"] == 0.5
    assert row["answer_path_alias_replacements"] == 2.0
    assert row["answer_schema_salvaged"] == 0.5
    assert row["answer_schema_salvage_locations"] == 3.0
    assert row["answer_location_order_normalized"] == 0.5
    assert row["answer_location_order_promotions"] == 1.0
    assert report_row["alias_norm%"] == "50%"
    assert report_row["alias_rep"] == "2.0"
    assert report_row["schema_salv%"] == "50%"
    assert report_row["schema_loc"] == "3.0"
    assert report_row["ord_norm%"] == "50%"
    assert report_row["ord_prom"] == "1.0"


def test_feedback_probe_backfills_answer_diagnosis_from_spans():
    cell = _cell("q1", "grep_only", 0.5, 1000)
    cell["answer"] = "Locations: src/a.py:1-2, src/noise.py:1-2, src/b.py:5-6"
    cell["gt_code_blocks"] = [
        {"file": "src/a.py", "start": 1, "end": 2},
        {"file": "src/b.py", "start": 5, "end": 6},
    ]
    cell["answer_spans"] = [
        {"file": "src/a.py", "start": 1, "end": 2},
        {"file": "src/noise1.py", "start": 1, "end": 2},
        {"file": "src/noise2.py", "start": 1, "end": 2},
        {"file": "src/noise3.py", "start": 1, "end": 2},
        {"file": "src/noise4.py", "start": 1, "end": 2},
        {"file": "src/b.py", "start": 5, "end": 6},
    ]

    agg = probe.aggregate([cell])
    row = agg["by_arm"]["grep_only"]

    assert row["answer_diag_rank_gap"] == 1.0
    assert _report_row(probe.render(agg), "grep_only")["diag"] == "rank:100%"


def test_feedback_probe_reports_answer_evidence_coverage():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            1200,
            answer_span_evidence=[
                {
                    "span": {"file": "src/a.py", "start": 10, "end": 20},
                    "trace_evidenced": True,
                    "read_confirmed": True,
                    "evidence": [{"source": "read", "state": "read"}],
                },
                {
                    "span": {"file": "src/b.py", "start": 30, "end": 40},
                    "trace_evidenced": True,
                    "read_confirmed": False,
                    "evidence": [
                        {
                            "source": "scheduled_graph_lsp",
                            "state": "offered",
                            "operation": "lsp_route",
                        }
                    ],
                },
                {
                    "span": {"file": "src/c.py", "start": 50, "end": 60},
                    "trace_evidenced": False,
                    "read_confirmed": False,
                    "evidence": [],
                },
            ],
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["answer_span_evidence_count"] == 3.0
    assert arm["answer_span_evidence_count_min"] == 3.0
    assert arm["answer_trace_evidence_rate"] == 2 / 3
    assert arm["answer_trace_evidence_rate_min"] == 2 / 3
    assert arm["answer_read_confirmed_rate"] == 1 / 3
    assert arm["answer_read_confirmed_rate_min"] == 1 / 3
    assert arm["answer_unconfirmed"] == 1.0
    assert arm["answer_unconfirmed_max"] == 1.0

    row = _report_row(probe.render(agg), "preinj_graph_scheduled")
    assert row["ans"] == "3.0"
    assert row["te%"] == "67%"
    assert row["te_min%"] == "67%"
    assert row["rc%"] == "33%"
    assert row["rc_min%"] == "33%"
    assert row["unc"] == "1.0"
    assert row["unc_max"] == "1.0"


def test_feedback_probe_answer_evidence_gate_passes(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell(
                "q1",
                "preinj_graph_scheduled",
                1.0,
                900,
                answer_span_evidence=[
                    {
                        "span": {"file": "src/a.py", "start": 10, "end": 20},
                        "trace_evidenced": True,
                        "read_confirmed": True,
                        "evidence": [{"source": "read", "state": "read"}],
                    },
                    {
                        "span": {"file": "src/b.py", "start": 30, "end": 40},
                        "trace_evidenced": True,
                        "read_confirmed": False,
                        "evidence": [{"source": "scheduled_graph_lsp"}],
                    },
                ],
            )
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-answer-evidence",
            "preinj_graph_scheduled",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "answer_evidence_gate.json").read_text())
    row = _answer_evidence_gate_row(
        (output_dir / "feedback_probe.md").read_text(),
        "preinj_graph_scheduled",
    )

    assert rc == 0
    assert captured.err == ""
    assert gate["status"] == "pass"
    assert gate["promote"] is True
    assert gate["criteria"]["min_trace_evidence_rate"] == 1.0
    assert gate["criteria"]["min_read_confirmed_rate"] is None
    assert row["status"] == "pass"
    assert row["ans_min"] == "2.0"
    assert row["te_min%"] == "100%"
    assert row["rc_min%"] == "50%"
    assert row["unc_max"] == "0.0"


def test_feedback_probe_answer_evidence_gate_fails_unconfirmed_span(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell(
                "q1",
                "preinj_graph_scheduled",
                1.0,
                900,
                answer_span_evidence=[
                    {
                        "span": {"file": "src/a.py", "start": 10, "end": 20},
                        "trace_evidenced": True,
                        "read_confirmed": True,
                        "evidence": [{"source": "read", "state": "read"}],
                    },
                    {
                        "span": {"file": "src/z.py", "start": 50, "end": 60},
                        "trace_evidenced": False,
                        "read_confirmed": False,
                        "evidence": [],
                    },
                ],
            )
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-answer-evidence",
            "preinj_graph_scheduled",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "answer_evidence_gate.json").read_text())

    assert rc == 2
    assert "answer evidence gate failed" in captured.err
    assert "preinj_graph_scheduled:trace_evidence_below_threshold" in captured.err
    assert "preinj_graph_scheduled:unconfirmed_answer_spans" in captured.err
    assert gate["status"] == "fail"
    assert gate["promote"] is False


def test_feedback_probe_answer_evidence_gate_can_require_read_confirmation(
    tmp_path, capsys
):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell(
                "q1",
                "preinj_graph_scheduled",
                1.0,
                900,
                answer_span_evidence=[
                    {
                        "span": {"file": "src/a.py", "start": 10, "end": 20},
                        "trace_evidenced": True,
                        "read_confirmed": False,
                        "evidence": [{"source": "scheduled_graph_lsp"}],
                    }
                ],
            )
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-answer-evidence",
            "preinj_graph_scheduled",
            "--answer-evidence-min-read-rate",
            "1.0",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "answer_evidence_gate.json").read_text())

    assert rc == 2
    assert "preinj_graph_scheduled:read_evidence_below_threshold" in captured.err
    assert gate["criteria"]["min_read_confirmed_rate"] == 1.0
    assert gate["blockers"] == ["preinj_graph_scheduled:read_evidence_below_threshold"]


def test_feedback_probe_replay_readiness_gate_passes(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [_with_exact_replay(_cell("q1", "preinj_eager_compact", 1.0, 800))],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-exact-replay",
            "preinj_eager_compact",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "replay_readiness_gate.json").read_text())
    row = _replay_gate_row(
        (output_dir / "feedback_probe.md").read_text(),
        "preinj_eager_compact",
    )

    assert rc == 0
    assert captured.err == ""
    assert gate["status"] == "pass"
    assert gate["promote"] is True
    assert gate["runs"][0]["evidence"]["exact_replayable_min"] == 1.0
    assert row["status"] == "pass"
    assert row["event_min%"] == "100%"
    assert row["exact_min%"] == "100%"
    assert row["gap_max"] == "0.0"
    assert row["blockers"] == "-"


def test_feedback_probe_replay_readiness_gate_fails_without_messages(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [_cell("q1", "preinj_eager_compact", 1.0, 800)],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-exact-replay",
            "preinj_eager_compact",
        ]
    )
    captured = capsys.readouterr()
    gate = json.loads((output_dir / "replay_readiness_gate.json").read_text())

    assert rc == 2
    assert "replay readiness gate failed" in captured.err
    assert "preinj_eager_compact:exact_replay_blocked" in captured.err
    assert "preinj_eager_compact:missing_message_transcript" in captured.err
    assert gate["status"] == "fail"
    assert gate["runs"][0]["evidence"]["blocking_gap_labels"] == [
        "missing_message_transcript"
    ]


def test_feedback_probe_haiku_compact_promotion_profile_passes(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell("q1", "grep_only", 1.0, 1000, turns=4),
            _with_exact_replay(
                _cell(
                    "q1",
                    "preinj_eager_compact",
                    1.0,
                    800,
                    compact=True,
                    turns=3,
                    answer_span_evidence=[
                        {
                            "span": {"file": "src/a.py", "start": 10, "end": 20},
                            "trace_evidenced": True,
                            "read_confirmed": True,
                            "evidence": [{"source": "read", "state": "read"}],
                        }
                    ],
                )
            ),
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--promotion-profile",
            "haiku_compact",
        ]
    )
    captured = capsys.readouterr()
    profile_gate = json.loads((output_dir / "promotion_profile_gate.json").read_text())
    feedback_gate = json.loads(
        (output_dir / "feedback_iteration_gate.json").read_text()
    )
    answer_gate = json.loads((output_dir / "answer_evidence_gate.json").read_text())
    replay_gate = json.loads((output_dir / "replay_readiness_gate.json").read_text())
    decision = json.loads((output_dir / "promotion_decision.json").read_text())
    row = _promotion_profile_gate_row(
        (output_dir / "feedback_probe.md").read_text(),
        "haiku_compact",
    )
    decision_row = _promotion_decision_row(
        (output_dir / "feedback_probe.md").read_text()
    )

    assert rc == 0
    assert captured.err == ""
    assert profile_gate["status"] == "pass"
    assert profile_gate["promote"] is True
    assert feedback_gate["criteria"]["require_savings"] is True
    assert feedback_gate["promote"] is True
    assert answer_gate["promote"] is True
    assert replay_gate["promote"] is True
    assert decision["decision"] == "promote"
    assert decision["promote"] is True
    assert decision["action"] == "promote_harness_slice"
    assert row["status"] == "pass"
    assert row["gates"] == "feedback_iteration,answer_evidence,replay_readiness"
    assert row["blockers"] == "-"
    assert decision_row["decision"] == "promote"
    assert decision_row["promote"] == "yes"
    assert decision_row["action"] == "promote_harness_slice"


def test_feedback_probe_haiku_compact_promotion_profile_fails(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    _write_cells(
        cells_dir,
        [
            _cell("q1", "grep_only", 1.0, 1000, turns=3),
            _cell(
                "q1",
                "preinj_eager_compact",
                1.0,
                1200,
                compact=True,
                turns=3,
                answer_span_evidence=[
                    {
                        "span": {"file": "src/z.py", "start": 50, "end": 60},
                        "trace_evidenced": False,
                        "read_confirmed": False,
                        "evidence": [],
                    }
                ],
            ),
        ],
    )

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--promotion-profile",
            "haiku_compact",
        ]
    )
    captured = capsys.readouterr()
    profile_gate = json.loads((output_dir / "promotion_profile_gate.json").read_text())
    decision = json.loads((output_dir / "promotion_decision.json").read_text())
    decision_row = _promotion_decision_row(
        (output_dir / "feedback_probe.md").read_text()
    )

    assert rc == 2
    assert "feedback iteration gate failed" in captured.err
    assert "answer evidence gate failed" in captured.err
    assert "replay readiness gate failed" in captured.err
    assert "promotion profile gate failed" in captured.err
    assert profile_gate["status"] == "fail"
    assert profile_gate["promote"] is False
    assert decision["decision"] == "revise"
    assert decision["promote"] is False
    assert decision["action"] == "complete_replay_artifacts"
    assert decision["next_actions"] == [
        "complete_replay_artifacts",
        "repair_answer_evidence",
        "reduce_tokens_or_turns",
    ]
    assert decision_row["decision"] == "revise"
    assert decision_row["promote"] == "no"
    assert decision_row["action"] == "complete_replay_artifacts"
    assert (
        "haiku_compact:feedback:preinj_eager_compact:no_token_or_turn_savings"
        in profile_gate["blockers"]
    )
    assert (
        "haiku_compact:answer_evidence:"
        "preinj_eager_compact:trace_evidence_below_threshold"
    ) in profile_gate["blockers"]
    assert (
        "haiku_compact:answer_evidence:" "preinj_eager_compact:unconfirmed_answer_spans"
    ) in profile_gate["blockers"]
    assert (
        "haiku_compact:replay:preinj_eager_compact:missing_message_transcript"
        in profile_gate["blockers"]
    )


def test_feedback_probe_lists_promotion_profiles(capsys):
    rc = probe.main(["--list-promotion-profiles"])
    captured = capsys.readouterr()
    profiles = json.loads(captured.out)

    assert rc == 0
    assert captured.err == ""
    assert "haiku_compact" in profiles
    assert profiles["haiku_compact"]["feedback_arms"] == ["preinj_eager_compact"]
    assert profiles["haiku_compact"]["replay_arms"] == ["preinj_eager_compact"]


def test_feedback_probe_reports_repeat_variance():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell("q1", "preinj_graph_scheduled", 1.0, 1200, turns=3),
        _cell("q1", "preinj_graph_scheduled", 0.5, 1800, turns=6),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["n"] == 1
    assert arm["reps"] == 2.0
    assert arm["rec5"] == 0.75
    assert arm["rec5_min"] == 0.5
    assert arm["tokens"] == 1200.0
    assert arm["tokens_max"] == 1800.0
    assert arm["tokens_spread"] == 600.0
    assert arm["turns"] == 3.0
    assert arm["turns_max"] == 6.0
    assert arm["turns_spread"] == 3.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["rep_n"] == "2.0"
    assert row["rec_min"] == "0.500"
    assert row["tok_hi"] == "1800"
    assert row["tok_rng"] == "600"
    assert row["turn_hi"] == "6.0"
    assert row["turn_rng"] == "3.0"


def test_feedback_probe_reports_read_overlap_pressure():
    cells = [
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            1200,
            file_reads=[
                {"file_path": "src/a.py", "offset": 10, "limit": 5},
                {"file_path": "src/a.py", "offset": 12, "limit": 5},
                {"file_path": "src/a.py", "offset": 10, "limit": 2},
                {"file_path": "src/b.py", "offset": 1, "limit": 3},
            ],
        )
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["read_repeat_offsets"] == 1.0
    assert arm["read_overlap_events"] == 2.0
    assert arm["read_overlap_lines"] == 5.0
    assert round(arm["read_overlap_rate"], 3) == 0.333

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["rrep"] == "1.0"
    assert row["rovlp%"] == "33%"
    assert row["rovlp_l"] == "5.0"


def test_feedback_probe_buckets_read_overlap_by_route_phase():
    cells = [
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            1200,
            scheduled_context=True,
            scheduled_operation="lsp_route",
            scheduled_final_guard=True,
            file_reads=[
                {"file_path": "src/a.py", "offset": 10, "limit": 5, "turn": 1},
                {"file_path": "src/a.py", "offset": 12, "limit": 5, "turn": 2},
                {"file_path": "src/a.py", "offset": 14, "limit": 5, "turn": 3},
                {"file_path": "src/a.py", "offset": 16, "limit": 5, "turn": 5},
            ],
        )
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["read_overlap_lines"] == 9.0
    assert arm["read_overlap_pre_route_events"] == 1.0
    assert arm["read_overlap_pre_route_lines"] == 3.0
    assert arm["read_overlap_post_route_events"] == 2.0
    assert arm["read_overlap_post_route_lines"] == 6.0
    assert arm["read_overlap_post_guard_events"] == 1.0
    assert arm["read_overlap_post_guard_lines"] == 3.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["rov_pre_l"] == "3.0"
    assert row["rov_post_l"] == "6.0"
    assert row["rov_fg_l"] == "3.0"


def test_feedback_probe_classifies_post_route_read_loop_shapes():
    cells = [
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            1200,
            scheduled_context=True,
            scheduled_operation="lsp_route",
            file_reads=[
                {"file_path": "src/a.py", "offset": 10, "limit": 10, "turn": 1},
                {"file_path": "src/a.py", "offset": 10, "limit": 15, "turn": 3},
                {"file_path": "src/a.py", "offset": 12, "limit": 5, "turn": 4},
                {"file_path": "src/a.py", "offset": 20, "limit": 10, "turn": 5},
            ],
        )
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["read_overlap_post_route_lines"] == 20.0
    assert arm["read_overlap_post_route_expansion_events"] == 1.0
    assert arm["read_overlap_post_route_contained_events"] == 1.0
    assert arm["read_overlap_post_route_shifted_events"] == 1.0
    assert arm["read_overlap_post_route_low_novelty_events"] == 1.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["post_exp"] == "1.0"
    assert row["post_sub"] == "1.0"
    assert row["post_shift"] == "1.0"
    assert row["post_low"] == "1.0"


def test_feedback_probe_derives_read_phase_from_trace_for_old_cells():
    cell = _cell(
        "q1",
        "preinj_graph_scheduled",
        1.0,
        1200,
        scheduled_context=True,
        scheduled_operation="lsp_route",
        scheduled_final_guard=True,
        file_reads=[
            {"file_path": "src/a.py", "offset": 10, "limit": 5},
            {"file_path": "src/a.py", "offset": 12, "limit": 5},
            {"file_path": "src/a.py", "offset": 14, "limit": 5},
            {"file_path": "src/a.py", "offset": 16, "limit": 5},
        ],
    )
    route_event = next(
        e
        for e in cell["trace"]["events"]
        if (e.get("data") or {}).get("operation") == "lsp_route"
    )
    guard_event = next(
        e
        for e in cell["trace"]["events"]
        if (e.get("data") or {}).get("operation") == "lsp_route_finalization_guard"
    )

    def tool_event(turn, offset):
        return {
            "kind": "tool_call",
            "turn": turn,
            "data": {
                "tool": "read",
                "tool_call_id": f"call_{turn}_{offset}",
                "arguments": {
                    "file_path": "src/a.py",
                    "offset": offset,
                    "limit": 5,
                },
                "error": None,
            },
        }

    cell["trace"]["events"] = [
        {"kind": "run_start", "turn": 0, "data": {}},
        tool_event(1, 10),
        tool_event(2, 12),
        route_event,
        tool_event(3, 14),
        guard_event,
        tool_event(5, 16),
        {"kind": "run_end", "turn": 5, "data": {}},
    ]

    agg = probe.aggregate([cell])

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["read_overlap_lines"] == 9.0
    assert arm["read_overlap_pre_route_lines"] == 3.0
    assert arm["read_overlap_post_route_lines"] == 6.0
    assert arm["read_overlap_post_guard_lines"] == 3.0


def test_feedback_probe_reports_wrong_preload_fallback():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_eager",
            0.5,
            900,
            fallback=True,
            verified_preload=False,
        ),
    ]

    agg = probe.aggregate(cells)

    eager = agg["by_arm"]["preinj_eager"]
    assert eager["fallback"] == 1.0
    assert eager["preload"] == 2.0
    assert eager["preload_verified"] == 0.0
    assert eager["preload_verify_rate"] == 0.0

    report = probe.render(agg)
    assert "| preinj_eager | 1 |" in report
    assert "| preinj_eager | 1 | 0.500 |" in report
    assert "| preinj_eager | 1 | 0.000 | -100 |" in report
    assert _report_row(report, "preinj_eager")["fb%"] == "100%"


def test_feedback_probe_reports_graph_expansion_events():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_eager_compact",
            0.5,
            800,
            compact=True,
            graph_expansion=True,
        ),
    ]

    agg = probe.aggregate(cells)

    compact = agg["by_arm"]["preinj_eager_compact"]
    assert compact["graph_expansion"] == 1.0
    assert compact["graph_expansion_nodes"] == 3.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_eager_compact")
    assert row["gexp%"] == "100%"
    assert row["gexp_n"] == "3.0"


def test_feedback_probe_reports_lsp_tool_use():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "lsp_graph",
            0.5,
            800,
            lsp=True,
            lsp_source="lsp_route",
            lsp_route_tool_turn=3,
            lsp_route_tool_results=12,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["lsp_graph"]
    assert arm["lsp"] == 1.0
    assert arm["lsp_used"] == 1.0
    assert arm["lsp_route_tool_used"] == 1.0
    assert arm["lsp_route_tool_first_turn"] == 3.0
    assert arm["lsp_route_tool_results"] == 12.0
    assert agg["tool_envelope_plan"]["lsp_graph"]["status"] == "incomplete"
    assert agg["tool_envelope_plan"]["lsp_graph"]["action"] == (
        "complete_tool_envelope_coverage"
    )

    report = probe.render(agg)
    row = _report_row(report, "lsp_graph")
    tool_row = _tool_plan_row(report, "lsp_graph")
    assert row["lsp"] == "1.0"
    assert row["lsp%"] == "100%"
    assert row["lr%"] == "100%"
    assert row["lrturn"] == "3.0"
    assert row["lrres"] == "12.0"
    assert tool_row["status"] == "incomplete"


def test_feedback_probe_consumes_typed_tool_envelopes():
    cell = _cell("q1", "lsp_graph", 0.5, 800, lsp=True, lsp_source="lsp_route")
    cell["trace"]["events"].extend(
        [
            {
                "kind": "tool_call",
                "turn": 1,
                "data": {
                    "tool": "read",
                    "error": None,
                    "result_chars": 1,
                    "envelope": {
                        "status": "ok",
                        "result_type": "text",
                        "result_chars": 300,
                        "duration_ms": 1.0,
                    },
                },
            },
            {
                "kind": "tool_call",
                "turn": 2,
                "data": {
                    "tool": "lsp_route",
                    "error": None,
                    "n_results": 99,
                    "envelope": {
                        "status": "error",
                        "result_type": "error",
                        "result_count": 7,
                        "result_chars": 2000,
                        "duration_ms": 2.0,
                        "truncated": True,
                        "error_kind": "timeout",
                    },
                },
            },
        ]
    )

    agg = probe.aggregate([cell])
    arm = agg["by_arm"]["lsp_graph"]
    report = probe.render(agg)
    row = _report_row(report, "lsp_graph")
    tool_row = _tool_plan_row(report, "lsp_graph")

    assert arm["tool_calls"] == 2.0
    assert arm["tool_envelope_coverage"] == 1.0
    assert arm["tool_errors"] == 1.0
    assert arm["tool_truncated"] == 1.0
    assert arm["tool_result_chars"] == 2300.0
    assert arm["lsp_route_tool_results"] == 7.0
    assert agg["tool_envelope_plan"]["lsp_graph"]["status"] == "fail"
    assert agg["tool_envelope_plan"]["lsp_graph"]["action"] == (
        "inspect_typed_tool_errors"
    )
    assert row["tool_n"] == "2.0"
    assert row["tenv%"] == "100%"
    assert row["terr"] == "1.0"
    assert row["tskip"] == "0.0"
    assert row["ttrunc"] == "1.0"
    assert row["tchars_k"] == "2.3"
    assert row["lrres"] == "7.0"
    assert tool_row["status"] == "fail"
    assert tool_row["action"] == "inspect_typed_tool_errors"
    assert tool_row["tenv%"] == "100%"


def test_feedback_probe_requires_tool_envelope_health(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    missing = _cell("q1", "grep_only", 1.0, 1000)
    healthy = _cell("q2", "lsp_graph", 1.0, 800)
    healthy["trace"]["events"].append(
        {
            "kind": "tool_call",
            "turn": 1,
            "data": {
                "tool": "read",
                "error": None,
                "envelope": {
                    "status": "ok",
                    "result_type": "text",
                    "result_chars": 300,
                    "duration_ms": 1.0,
                },
            },
        }
    )
    _write_cells(cells_dir, [missing, healthy])

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-tool-envelope-health",
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads((output_dir / "tool_envelope_plan.json").read_text())

    assert rc == 2
    assert "tool envelope health gate failed" in captured.err
    assert plan["grep_only"]["status"] == "missing"


def test_feedback_probe_tool_envelope_health_gate_passes(tmp_path, capsys):
    cells_dir = tmp_path / "cells"
    output_dir = tmp_path / "out"
    healthy = _cell("q1", "grep_only", 1.0, 1000)
    healthy["trace"]["events"].append(
        {
            "kind": "tool_call",
            "turn": 1,
            "data": {
                "tool": "read",
                "error": None,
                "envelope": {
                    "status": "ok",
                    "result_type": "text",
                    "result_chars": 300,
                    "duration_ms": 1.0,
                },
            },
        }
    )
    _write_cells(cells_dir, [healthy])

    rc = probe.main(
        [
            "--cells-dir",
            str(cells_dir),
            "--output-dir",
            str(output_dir),
            "--require-tool-envelope-health",
        ]
    )
    captured = capsys.readouterr()
    plan = json.loads((output_dir / "tool_envelope_plan.json").read_text())

    assert rc == 0
    assert captured.err == ""
    assert plan["grep_only"]["status"] == "pass"


def test_feedback_probe_reports_scheduled_context_events():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            0.5,
            850,
            scheduled_context=True,
            scheduled_operation="lsp_route",
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_context"] == 1.0
    assert arm["scheduled_context_nodes"] == 2.0
    assert arm["scheduled_lsp_route"] == 1.0
    assert arm["scheduled_lsp_definition"] == 0.0
    assert arm["scheduled_final_guard"] == 0.0
    assert arm["scheduled_final_guard_nodes"] == 0.0
    assert arm["scheduled_graph_fanout"] == 0.0
    assert arm["scheduled_unknown_operation"] == 0.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["sched%"] == "100%"
    assert row["sched_n"] == "2.0"
    assert row["route%"] == "100%"
    assert row["sdef%"] == "0%"
    assert row["fg%"] == "0%"
    assert row["fg_n"] == "0.0"
    assert row["sfan%"] == "0%"
    assert row["op?%"] == "0%"


def test_feedback_probe_reports_scheduled_route_budget():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            850,
            scheduled_context=True,
            scheduled_operation="lsp_route",
            scheduled_route_budget_applied=True,
            scheduled_route_budget_dropped=4,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_route_budget_applied"] == 1.0
    assert arm["scheduled_route_budget_dropped"] == 4.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["rb%"] == "100%"
    assert row["rb_drop"] == "4.0"


def test_feedback_probe_reports_route_read_memory_counts():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            850,
            scheduled_context=True,
            scheduled_operation="lsp_route",
            scheduled_route_read_calls=4,
            scheduled_route_generic_min_reads=4,
            scheduled_route_bridge_query=True,
            scheduled_route_fanout_hold_count=1,
            scheduled_route_fanout_hold_first_turn=2,
            scheduled_route_fanout_hold_read_calls=1,
            scheduled_route_fanout_hold_search_calls=2,
            scheduled_route_fanout_hold_generic_min_reads=4,
            scheduled_route_read_confirmed_count=3,
            scheduled_route_read_confirmed_core_count=2,
            scheduled_route_read_confirmed_protected_count=1,
            scheduled_route_unread_core_count=4,
            scheduled_route_unread_protected_count=2,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_route_read_confirmed"] == 3.0
    assert arm["scheduled_route_read_confirmed_core"] == 2.0
    assert arm["scheduled_route_read_confirmed_protected"] == 1.0
    assert arm["scheduled_route_unread_core"] == 4.0
    assert arm["scheduled_route_unread_protected"] == 2.0
    assert arm["scheduled_route_trigger_turn"] == 2.0
    assert arm["scheduled_route_read_calls"] == 4.0
    assert arm["scheduled_route_generic_min_reads"] == 4.0
    assert arm["scheduled_route_bridge_query"] == 1.0
    assert arm["scheduled_route_fanout_hold_count"] == 1.0
    assert arm["scheduled_route_fanout_hold_first_turn"] == 2.0
    assert arm["scheduled_route_fanout_hold_read_calls"] == 1.0
    assert arm["scheduled_route_fanout_hold_search_calls"] == 2.0
    assert arm["scheduled_route_fanout_hold_generic_min_reads"] == 4.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["rturn"] == "2.0"
    assert row["rcalls"] == "4.0"
    assert row["rgmin"] == "4.0"
    assert row["rbridge%"] == "100%"
    assert row["rhold"] == "1.0"
    assert row["rhturn"] == "2.0"
    assert row["rhcalls"] == "1.0"
    assert row["rread"] == "3.0"
    assert row["rread_c"] == "2.0"
    assert row["rkeep"] == "1.0"
    assert row["runread_c"] == "4.0"
    assert row["runkeep"] == "2.0"


def test_feedback_probe_reconstructs_route_read_memory_for_old_traces():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        {
            "query_id": "q1",
            "subset_id": "preinj_graph_scheduled",
            "category": "cat",
            "success": True,
            "metrics": {"answer_blocks": {5: {"recall": 1.0}}},
            "total_tokens": 850,
            "total_turns": 2,
            "trace": {
                "context": [],
                "events": [
                    {
                        "kind": "tool_call",
                        "turn": 1,
                        "data": {
                            "tool": "read",
                            "arguments": {
                                "file_path": "health.go",
                                "offset": 20,
                                "limit": 6,
                            },
                        },
                    },
                    {
                        "kind": "scheduled_context",
                        "turn": 2,
                        "data": {
                            "source": "scheduled_graph_lsp",
                            "reason": "read_symbols",
                            "operation": "lsp_route",
                            "locations": [
                                {
                                    "file": "health.go",
                                    "start_line": 20,
                                    "end_line": 25,
                                    "name": "Handler#doActiveHealthCheckForAllHosts",
                                    "role": "endpoint",
                                    "source": "direct_definition",
                                },
                                {
                                    "file": "replacer.go",
                                    "start_line": 110,
                                    "end_line": 125,
                                    "name": "globalDefaultReplacements",
                                    "role": "provider",
                                    "source": "route_neighbor",
                                },
                                {
                                    "file": "replacer.go",
                                    "start_line": 29,
                                    "end_line": 47,
                                    "name": "NewReplacer",
                                    "role": "bridge",
                                    "source": "direct_definition",
                                },
                            ],
                            "auto_open_locations": [
                                {
                                    "file": "replacer.go",
                                    "start_line": 110,
                                    "end_line": 112,
                                    "name": "globalDefaultReplacements",
                                    "role": "provider",
                                    "source": "route_neighbor",
                                }
                            ],
                        },
                    },
                ],
            },
        },
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_route_read_confirmed"] == 2.0
    assert arm["scheduled_route_read_confirmed_core"] == 2.0
    assert arm["scheduled_route_read_confirmed_protected"] == 1.0
    assert arm["scheduled_route_unread_core"] == 1.0
    assert arm["scheduled_route_unread_protected"] == 1.0


def test_feedback_probe_reports_answer_cited_unread_route_core():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        {
            "query_id": "q1",
            "subset_id": "preinj_graph_scheduled",
            "category": "cat",
            "success": True,
            "metrics": {"answer_blocks": {5: {"recall": 1.0}}},
            "total_tokens": 850,
            "total_turns": 2,
            "answer": (
                "Locations:\\n"
                "- health.go:20-25\\n"
                "- replacer.go:29-47\\n"
                "- replacer.go:110-125\\n"
                "- other.go:1-5\\n"
                "- tail.go:50-60\\n"
                "- replacer.go:140-160\\n"
            ),
            "trace": {
                "context": [],
                "events": [
                    {
                        "kind": "tool_call",
                        "turn": 1,
                        "data": {
                            "tool": "read",
                            "arguments": {
                                "file_path": "health.go",
                                "offset": 20,
                                "limit": 6,
                            },
                        },
                    },
                    {
                        "kind": "scheduled_context",
                        "turn": 2,
                        "data": {
                            "source": "scheduled_graph_lsp",
                            "reason": "read_symbols",
                            "operation": "lsp_route",
                            "locations": [
                                {
                                    "file": "health.go",
                                    "start_line": 20,
                                    "end_line": 25,
                                    "name": "Handler#doActiveHealthCheckForAllHosts",
                                    "role": "endpoint",
                                    "source": "direct_definition",
                                },
                                {
                                    "file": "replacer.go",
                                    "start_line": 29,
                                    "end_line": 47,
                                    "name": "NewReplacer",
                                    "role": "bridge",
                                    "source": "direct_definition",
                                },
                                {
                                    "file": "replacer.go",
                                    "start_line": 110,
                                    "end_line": 125,
                                    "name": "globalDefaultReplacements",
                                    "role": "provider",
                                    "source": "route_neighbor",
                                },
                                {
                                    "file": "unused.go",
                                    "start_line": 1,
                                    "end_line": 1,
                                    "name": "unusedConstant",
                                    "role": "support",
                                    "source": "direct_definition",
                                },
                                {
                                    "file": "unused.go",
                                    "start_line": 2,
                                    "end_line": 2,
                                    "name": "unusedFlag",
                                    "role": "support",
                                    "source": "direct_definition",
                                },
                                {
                                    "file": "replacer.go",
                                    "start_line": 140,
                                    "end_line": 160,
                                    "name": "Replacer#ReplaceKnown",
                                    "role": "bridge",
                                    "source": "direct_definition",
                                },
                            ],
                        },
                    },
                ],
            },
        },
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_route_unread_core"] == 3.0
    assert arm["scheduled_route_unread_core_answer_cited"] == 3.0
    assert arm["scheduled_route_unread_core_answer_top5"] == 2.0
    assert arm["scheduled_route_unread_core_answer_route_top5"] == 2.0
    assert arm["scheduled_route_unread_core_answer_route_tail"] == 1.0

    row = _report_row(probe.render(agg), "preinj_graph_scheduled")
    assert row["runans"] == "3.0"
    assert row["runans5"] == "2.0"
    assert row["runr5"] == "2.0"
    assert row["runrtail"] == "1.0"


def test_feedback_probe_reports_unsafe_route_budget():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            850,
            scheduled_context=True,
            scheduled_operation="lsp_route",
            scheduled_route_budget_reason="unsafe_missing_core",
            scheduled_route_budget_missing_required_count=2,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_route_budget_unsafe"] == 1.0
    assert arm["scheduled_route_budget_missing_required"] == 2.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["rb_unsafe%"] == "100%"
    assert row["rb_miss"] == "2.0"


def test_feedback_probe_reports_scheduled_final_guard():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            0.5,
            850,
            scheduled_context=True,
            scheduled_operation="lsp_route",
            scheduled_final_guard=True,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_final_guard"] == 1.0
    assert arm["scheduled_final_guard_nodes"] == 3.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["fg%"] == "100%"
    assert row["fg_n"] == "3.0"


def test_feedback_probe_reports_unknown_scheduled_operation():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell("q1", "preinj_graph_scheduled", 0.5, 850, scheduled_context=True),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_lsp_route"] == 0.0
    assert arm["scheduled_unknown_operation"] == 1.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["route%"] == "0%"
    assert row["op?%"] == "100%"


def test_feedback_probe_reports_scheduled_context_skips():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            0.5,
            850,
            scheduled_operation="lsp_route",
            scheduled_skipped=True,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_context"] == 0.0
    assert arm["scheduled_lsp_route"] == 0.0
    assert arm["scheduled_context_skipped"] == 1.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["sched%"] == "0%"
    assert row["route%"] == "0%"
    assert row["sskip%"] == "100%"


def test_feedback_probe_reports_scheduled_anchor_audit():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            0.5,
            850,
            scheduled_context=True,
            scheduled_anchor_audit=True,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_anchor_audit"] == 1.0
    assert arm["scheduled_anchor_audit_offered"] == 3.0
    assert arm["scheduled_anchor_audit_read"] == 2.0
    assert arm["scheduled_anchor_audit_cited"] == 0.0

    report = probe.render(agg)
    assert _report_row(report, "preinj_graph_scheduled")["aud%"] == "100%"


def test_feedback_probe_reports_scheduled_top_and_ordering_audits():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            0.5,
            850,
            scheduled_context=True,
            scheduled_top_anchor_audit=True,
            scheduled_top_anchor_audit_missing_unread=1,
            scheduled_top_anchor_audit_answer_only=True,
            scheduled_ordering_audit=True,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_top_anchor_audit"] == 1.0
    assert arm["scheduled_top_anchor_audit_missing"] == 1.0
    assert arm["scheduled_top_anchor_audit_missing_unread"] == 1.0
    assert arm["scheduled_top_anchor_audit_answer_only"] == 1.0
    assert arm["scheduled_ordering_audit"] == 1.0
    assert arm["scheduled_ordering_audit_spans"] == 8.0

    report = probe.render(agg)
    row = _report_row(report, "preinj_graph_scheduled")
    assert row["taud%"] == "100%"
    assert row["taud_m"] == "1.0"
    assert row["taud_u"] == "1.0"
    assert row["taud_ao%"] == "100%"
    assert row["taud_bt%"] == "0%"
    assert row["taud_dp%"] == "0%"
    assert row["taud_ro%"] == "0%"
    assert row["taud_fb%"] == "0%"
    assert row["ord%"] == "100%"
    assert row["ord_n"] == "8.0"


def test_feedback_probe_reports_bounded_top_anchor_correction():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            900,
            scheduled_context=True,
            scheduled_top_anchor_audit=True,
            scheduled_top_anchor_audit_bounded_tool_loop=True,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_top_anchor_audit"] == 1.0
    assert arm["scheduled_top_anchor_audit_answer_only"] == 0.0
    assert arm["scheduled_top_anchor_audit_bounded_tool_loop"] == 1.0

    row = _report_row(probe.render(agg), "preinj_graph_scheduled")
    assert row["taud%"] == "100%"
    assert row["taud_ao%"] == "0%"
    assert row["taud_bt%"] == "100%"
    assert row["taud_dp%"] == "0%"
    assert row["taud_ro%"] == "0%"
    assert row["taud_fb%"] == "0%"


def test_feedback_probe_reports_deterministic_top_anchor_patch():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            900,
            scheduled_context=True,
            scheduled_top_anchor_audit=True,
            scheduled_top_anchor_audit_deterministic_patch=True,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_top_anchor_audit"] == 1.0
    assert arm["scheduled_top_anchor_audit_bounded_tool_loop"] == 0.0
    assert arm["scheduled_top_anchor_audit_deterministic_patch"] == 1.0
    assert arm["scheduled_top_anchor_audit_validated_deterministic"] == 1.0
    assert arm["scheduled_top_anchor_audit_unvalidated"] == 0.0

    row = _report_row(probe.render(agg), "preinj_graph_scheduled")
    assert row["taud%"] == "100%"
    assert row["taud_bt%"] == "0%"
    assert row["taud_dp%"] == "100%"
    assert row["taud_vdp%"] == "100%"
    assert row["taud_bad%"] == "0%"
    assert row["taud_ro%"] == "0%"
    assert row["taud_fb%"] == "0%"


def test_feedback_probe_reports_read_only_top_anchor_correction():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            900,
            scheduled_context=True,
            scheduled_top_anchor_audit=True,
            scheduled_top_anchor_audit_read_only=True,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_top_anchor_audit"] == 1.0
    assert arm["scheduled_top_anchor_audit_read_only"] == 1.0

    row = _report_row(probe.render(agg), "preinj_graph_scheduled")
    assert row["taud%"] == "100%"
    assert row["taud_ro%"] == "100%"
    assert row["taud_fb%"] == "0%"


def test_feedback_probe_reports_cascade_fallback_attempt_mode():
    cells = [
        _cell("q1", "grep_only", 0.5, 1000),
        _cell(
            "q1",
            "preinj_graph_scheduled",
            1.0,
            900,
            scheduled_context=True,
            scheduled_top_anchor_audit=True,
            scheduled_top_anchor_audit_read_only=True,
            scheduled_top_anchor_audit_cascade_fallback=True,
        ),
    ]

    agg = probe.aggregate(cells)

    arm = agg["by_arm"]["preinj_graph_scheduled"]
    assert arm["scheduled_top_anchor_audit_read_only"] == 1.0
    assert arm["scheduled_top_anchor_audit_cascade_fallback"] == 1.0

    row = _report_row(probe.render(agg), "preinj_graph_scheduled")
    assert row["taud_ro%"] == "100%"
    assert row["taud_fb%"] == "100%"
