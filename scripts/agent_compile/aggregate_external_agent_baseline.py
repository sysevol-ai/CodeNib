# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate external agent JSONL baselines against a CodeMiner GT cell."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from codeminer.eval.retrieval_eval import (
    compute_block_metrics,
    compute_path_alias_metrics,
    dedup_spans,
    diagnose_answer_spans,
    parse_answer_spans,
    score_localization_spans,
)

KS = [1, 3, 5, 10]
_NATIVE_LSP_MARKERS = (
    "definition",
    "diagnostic",
    "document_symbol",
    "hover",
    "ide",
    "implementation",
    "language_server",
    "lsp",
    "reference",
    "workspace_symbol",
)
_GAP_REPAIR_CONTRACTS: Dict[str, Dict[str, str]] = {
    "answer_contract_gap": {
        "repair_action": "tighten_external_answer_contract",
        "repair_surface": "external_prompt_schema",
        "suggested_prompt_delta": (
            "Require single-line repo-relative Files/Symbols/Locations output, "
            "at most five Locations, and route-priority ordering before helpers."
        ),
    },
    "tool_adoption_gap": {
        "repair_action": "force_or_schedule_external_lsp_route",
        "repair_surface": "external_tool_adoption",
        "suggested_prompt_delta": (
            "Require a CodeMiner lsp_route call before broad grep/read when the "
            "query asks for endpoint, bridge, provider, or type-flow anchors."
        ),
    },
    "route_timing_gap": {
        "repair_action": "move_external_route_call_earlier",
        "repair_surface": "external_route_timing",
        "suggested_prompt_delta": (
            "Move lsp_route or native definition/reference lookup before repeated "
            "search/read loops; stop once endpoint, bridge, and provider anchors "
            "are read-confirmed."
        ),
    },
    "mcp_policy_gap": {
        "repair_action": "rerun_external_with_mcp_policy",
        "repair_surface": "external_mcp_policy",
        "suggested_prompt_delta": (
            "Use an execution policy where CodeMiner MCP calls are allowed to "
            "complete; do not compare read-only/no-approval runs that cancel MCP."
        ),
    },
    "mcp_transport_noise": {
        "repair_action": "inspect_external_mcp_transport",
        "repair_surface": "external_mcp_transport",
        "suggested_prompt_delta": (
            "Keep the same run contract but capture stderr and classify Internal "
            "Server Error noise separately from JSONL tool-call success."
        ),
    },
    "native_lsp_surface_missing": {
        "repair_action": "attach_native_lsp_surface",
        "repair_surface": "external_native_lsp_environment",
        "suggested_prompt_delta": (
            "Run the external agent in an IDE/LSP-attached environment and verify "
            "definition/reference tools appear in the init/tool trace."
        ),
    },
    "context_routing_gap": {
        "repair_action": "strengthen_external_context_routing",
        "repair_surface": "external_context_routing",
        "suggested_prompt_delta": (
            "Compare the external trace against the CodeMiner scheduled route "
            "anchors and add explicit route seeds or graph-MCP guidance."
        ),
    },
    "competitive": {
        "repair_action": "hold_external_contract",
        "repair_surface": "external_gate_recheck",
        "suggested_prompt_delta": (
            "No repair is required from the comparison matrix; keep the current "
            "contract and use the gate result as the promotion signal."
        ),
    },
}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _stderr_internal_errors(path: Path) -> int:
    stderr_path = path.with_suffix(".stderr")
    if not stderr_path.exists():
        return 0
    return sum(
        1
        for line in stderr_path.read_text(errors="replace").splitlines()
        if "Internal Server Error" in line
    )


def _claude_tool_names(rows: Iterable[Dict[str, Any]]) -> List[str]:
    tools: List[str] = []
    for row in rows:
        message = row.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                if name:
                    tools.append(name)
    return tools


def _claude_init_tools(rows: Iterable[Dict[str, Any]]) -> List[str]:
    for row in rows:
        if row.get("type") == "system" and row.get("subtype") == "init":
            return [str(tool) for tool in row.get("tools") or []]
    return []


def _codex_tool_names(rows: Iterable[Dict[str, Any]]) -> List[str]:
    tools: List[str] = []
    for row in rows:
        if row.get("type") != "item.started":
            continue
        item = row.get("item") or {}
        item_type = str(item.get("type") or "")
        if item_type == "mcp_tool_call":
            tools.append(f"mcp__{item.get('server')}__{item.get('tool')}")
        elif item_type == "command_execution":
            command = str(item.get("command") or "")
            shell = command.split()[0] if command else "command"
            tools.append(f"shell:{Path(shell).name}")
    return tools


def _codex_cancelled_mcp(rows: Iterable[Dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        item = row.get("item") or {}
        if row.get("type") != "item.completed" or item.get("type") != "mcp_tool_call":
            continue
        error = item.get("error") or {}
        if "cancelled" in str(error.get("message") or "").lower():
            count += 1
    return count


def _is_native_lsp_tool(name: str) -> bool:
    lowered = name.lower()
    if "codeminer" in lowered:
        return False
    return any(marker in lowered for marker in _NATIVE_LSP_MARKERS)


def _first_native_lsp_index(tools: List[str]) -> int | None:
    for index, tool in enumerate(tools, start=1):
        if _is_native_lsp_tool(tool):
            return index
    return None


def _native_lsp_surface_available(init_tools: List[str], tools: List[str]) -> bool:
    return any(_is_native_lsp_tool(tool) for tool in list(init_tools) + list(tools))


def _extract_claude(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    result = next((row for row in rows if row.get("type") == "result"), {})
    init_tools = _claude_init_tools(rows)
    tools = _claude_tool_names(rows)
    return {
        "format": "claude",
        "answer": str(result.get("result") or ""),
        "turns": result.get("num_turns"),
        "cost_usd": result.get("total_cost_usd"),
        "usage": result.get("usage") or {},
        "tools": tools,
        "init_tools": init_tools,
        "mcp_cancelled": 0,
    }


def _extract_codex(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    answer = ""
    for row in rows:
        item = row.get("item") or {}
        if row.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = str(item.get("text") or answer)
    usage = next(
        (row.get("usage") or {} for row in rows if row.get("type") == "turn.completed"),
        {},
    )
    return {
        "format": "codex",
        "answer": answer,
        "turns": None,
        "cost_usd": None,
        "usage": usage,
        "tools": _codex_tool_names(rows),
        "init_tools": [],
        "mcp_cancelled": _codex_cancelled_mcp(rows),
    }


def _detect_and_extract(path: Path) -> Dict[str, Any]:
    rows = _read_jsonl(path)
    if any(row.get("type") == "thread.started" for row in rows):
        return _extract_codex(rows)
    return _extract_claude(rows)


def _lsp_index(tools: List[str]) -> int | None:
    for index, tool in enumerate(tools, start=1):
        if tool.endswith("lsp_route"):
            return index
    return None


def _metric_bucket(metrics: Any, k: int) -> Dict[str, Any]:
    if not isinstance(metrics, dict):
        return {}
    bucket = metrics.get(k)
    if bucket is None:
        bucket = metrics.get(str(k))
    return bucket if isinstance(bucket, dict) else {}


def _metric_recall_at_5(row: Dict[str, Any]) -> float:
    try:
        return float(_metric_bucket(row.get("metrics"), 5).get("recall") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _all_recall(row: Dict[str, Any]) -> float:
    try:
        return float((row.get("answer_all_metrics") or {}).get("recall") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _tool_count_from_cell(cell: Dict[str, Any]) -> int:
    if isinstance(cell.get("tool_calls"), list):
        return len(cell["tool_calls"])
    events = (cell.get("trace") or {}).get("events") or []
    return sum(
        1
        for event in events
        if isinstance(event, dict) and event.get("kind") == "tool_call"
    )


def _cell_lsp_route_tool_index(cell: Dict[str, Any]) -> int | None:
    tool_calls = cell.get("tool_calls") or []
    for index, tool in enumerate(tool_calls, start=1):
        if isinstance(tool, dict) and str(tool.get("skill_id") or "") == "lsp_route":
            return index
    index = 0
    for event in (cell.get("trace") or {}).get("events") or []:
        if not isinstance(event, dict) or event.get("kind") != "tool_call":
            continue
        index += 1
        data = event.get("data") or {}
        if str(data.get("tool") or "") == "lsp_route":
            return index
    return None


def _scheduled_route_stats(cell: Dict[str, Any]) -> Dict[str, Any]:
    route_turns: List[int] = []
    final_guard_turns: List[int] = []
    operations: List[str] = []
    for event in (cell.get("trace") or {}).get("events") or []:
        if not isinstance(event, dict) or event.get("kind") != "scheduled_context":
            continue
        data = event.get("data") or {}
        operation = str(data.get("operation") or "").strip()
        if operation:
            operations.append(operation)
        try:
            turn = int(event.get("turn"))
        except (TypeError, ValueError):
            continue
        if operation == "lsp_route":
            route_turns.append(turn)
        elif operation == "lsp_route_finalization_guard":
            final_guard_turns.append(turn)
    top_operation = str(cell.get("scheduled_context_operation") or "").strip()
    if top_operation and top_operation not in operations:
        operations.append(top_operation)
    return {
        "scheduled_lsp_route": bool(route_turns or top_operation == "lsp_route"),
        "scheduled_lsp_route_turn": min(route_turns) if route_turns else None,
        "scheduled_final_guard": bool(final_guard_turns),
        "scheduled_final_guard_turn": (
            min(final_guard_turns) if final_guard_turns else None
        ),
        "scheduled_operations": operations,
    }


def _answer_span_count_from_cell(cell: Dict[str, Any]) -> int:
    for key in ("answer_span_count", "answer_top_spans", "answer_spans"):
        value = cell.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    return 0


def score_codeminer_cell(*, label: str, path: Path) -> Dict[str, Any]:
    cell = json.loads(path.read_text(encoding="utf-8"))
    route_stats = _scheduled_route_stats(cell)
    metrics = (cell.get("metrics") or {}).get("answer_blocks") or {}
    answer_all_metrics = cell.get("answer_all_metrics") or {}
    answer_path_alias_metrics = cell.get("answer_path_alias_metrics") or {}
    diagnosis = cell.get("answer_diagnosis")
    if not diagnosis:
        rec5 = _metric_recall_at_5({"metrics": metrics})
        diagnosis = diagnose_answer_spans(
            answer=str(cell.get("answer") or ""),
            deduped_spans=dedup_spans(cell.get("answer_spans") or []),
            rec_at_k=rec5,
            rec_all=float(answer_all_metrics.get("recall") or rec5),
            alias_recall=float(answer_path_alias_metrics.get("recall") or 0.0),
        )
    return {
        "label": label,
        "format": "codeminer_cell",
        "path": str(path),
        "query_id": cell.get("query_id"),
        "subset_id": cell.get("subset_id"),
        "turns": cell.get("total_turns"),
        "tokens": cell.get("total_tokens"),
        "tool_count": _tool_count_from_cell(cell),
        "lsp_route_tool_index": _cell_lsp_route_tool_index(cell),
        "native_lsp_available": False,
        "native_lsp_tool_index": None,
        "mcp_cancelled": 0,
        "mcp_stderr_internal_errors": 0,
        "output_tokens": None,
        "answer_span_count": _answer_span_count_from_cell(cell),
        "answer_all_metrics": answer_all_metrics,
        "answer_path_alias_metrics": answer_path_alias_metrics,
        "answer_diagnosis": diagnosis,
        "metrics": metrics,
        **route_stats,
    }


def score_run(
    *,
    label: str,
    path: Path,
    gt_blocks: List[Dict[str, Any]],
    repo_path: str = "",
) -> Dict[str, Any]:
    extracted = _detect_and_extract(path)
    spans = parse_answer_spans(extracted["answer"], repo_path)
    metrics = score_localization_spans(
        answer_spans=spans,
        retrieval_spans=[],
        gt_blocks=gt_blocks,
        ks=KS,
    )["answer_blocks"]
    deduped_spans = dedup_spans(spans)
    all_metrics = compute_block_metrics(deduped_spans, gt_blocks)
    alias_metrics = compute_path_alias_metrics(deduped_spans, gt_blocks)
    rec_at_5 = float(metrics[5].get("recall") or 0.0)
    rec_all = float(all_metrics.get("recall") or 0.0)
    tools = list(extracted["tools"])
    init_tools = list(extracted.get("init_tools") or [])
    usage = extracted.get("usage") or {}
    return {
        "label": label,
        "format": extracted["format"],
        "path": str(path),
        "turns": extracted.get("turns"),
        "tool_count": len(tools),
        "lsp_route_tool_index": _lsp_index(tools),
        "native_lsp_available": _native_lsp_surface_available(init_tools, tools),
        "native_lsp_tool_index": _first_native_lsp_index(tools),
        "mcp_cancelled": int(extracted.get("mcp_cancelled") or 0),
        "mcp_stderr_internal_errors": _stderr_internal_errors(path),
        "cost_usd": extracted.get("cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens")
        or usage.get("cache_read_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "answer_span_count": len(deduped_spans),
        "answer_all_metrics": all_metrics,
        "answer_path_alias_metrics": alias_metrics,
        "answer_diagnosis": diagnose_answer_spans(
            answer=extracted["answer"],
            deduped_spans=deduped_spans,
            rec_at_k=rec_at_5,
            rec_all=rec_all,
            alias_recall=float(alias_metrics.get("recall") or 0.0),
        ),
        "answer_spans": spans,
        "metrics": metrics,
        "init_tools": init_tools,
        "tools": tools,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(rows: List[Dict[str, Any]]) -> str:
    lines = [
        "# External Agent Baseline",
        "",
        (
            "| run | fmt | rec@5 | rec@all | spans | turns | tools | cm_lsp_idx | "
            "native_lsp | native_idx | mcp_cancel | stderr_err | cost | out_tok | "
            "diag |"
        ),
        (
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
            "--- | --- | --- | --- |"
        ),
    ]
    for row in rows:
        metric = (row.get("metrics") or {}).get(5) or {}
        all_metric = row.get("answer_all_metrics") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("label")),
                    str(row.get("format")),
                    _fmt(metric.get("recall")),
                    _fmt(all_metric.get("recall")),
                    _fmt(row.get("answer_span_count"), 0),
                    _fmt(row.get("turns"), 1),
                    _fmt(row.get("tool_count"), 0),
                    _fmt(row.get("lsp_route_tool_index"), 0),
                    "yes" if row.get("native_lsp_available") else "no",
                    _fmt(row.get("native_lsp_tool_index"), 0),
                    _fmt(row.get("mcp_cancelled"), 0),
                    _fmt(row.get("mcp_stderr_internal_errors"), 0),
                    _fmt(row.get("cost_usd")),
                    _fmt(row.get("output_tokens"), 0),
                    str(row.get("answer_diagnosis")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _gap_category(external: Dict[str, Any], reference: Dict[str, Any]) -> str:
    if int(external.get("mcp_cancelled") or 0) > 0:
        return "mcp_policy_gap"
    if int(external.get("mcp_stderr_internal_errors") or 0) > 0:
        return "mcp_transport_noise"
    ref_rec = _metric_recall_at_5(reference)
    ext_rec = _metric_recall_at_5(external)
    diagnosis = str(external.get("answer_diagnosis") or "")
    if ext_rec + 1e-9 < ref_rec:
        if diagnosis in {"rank_gap", "path_alias_gap", "format_gap", "empty_answer"}:
            return "answer_contract_gap"
        if (
            reference.get("scheduled_lsp_route")
            and external.get("lsp_route_tool_index") is None
        ):
            return "tool_adoption_gap"
        return "context_routing_gap"
    if (
        reference.get("scheduled_lsp_route")
        and external.get("lsp_route_tool_index") is None
    ):
        return "tool_adoption_gap"
    ref_turns = reference.get("turns")
    ext_turns = external.get("turns")
    if ref_turns is not None and ext_turns is not None:
        try:
            if float(ext_turns) > float(ref_turns):
                return "route_timing_gap"
        except (TypeError, ValueError):
            # Non-numeric turn counts provide no route-timing signal.
            ref_turns = ext_turns = None
    ref_tools = reference.get("tool_count")
    ext_tools = external.get("tool_count")
    if ref_tools is not None and ext_tools is not None:
        try:
            if float(ext_tools) > float(ref_tools):
                return "route_timing_gap"
        except (TypeError, ValueError):
            # Non-numeric tool counts provide no route-timing signal.
            ref_tools = ext_tools = None
    if (
        not external.get("native_lsp_available")
        and external.get("lsp_route_tool_index") is None
    ):
        return "native_lsp_surface_missing"
    return "competitive"


def build_comparison_matrix(
    *,
    external_rows: Sequence[Dict[str, Any]],
    codeminer_rows: Sequence[Dict[str, Any]],
    reference_label: Optional[str] = None,
) -> Dict[str, Any]:
    references = {str(row.get("label")): row for row in codeminer_rows}
    if reference_label:
        reference = references.get(reference_label)
    else:
        reference = codeminer_rows[0] if codeminer_rows else None
        reference_label = str(reference.get("label")) if reference else None
    if reference is None:
        return {
            "comparison_matrix_version": 1,
            "status": "missing_reference",
            "reference_label": reference_label,
            "rows": [],
        }

    rows: List[Dict[str, Any]] = []
    ref_rec = _metric_recall_at_5(reference)
    ref_all = _all_recall(reference)
    for external in external_rows:
        ext_rec = _metric_recall_at_5(external)
        ext_all = _all_recall(external)
        rows.append(
            {
                "label": external.get("label"),
                "format": external.get("format"),
                "reference_label": reference_label,
                "gap_category": _gap_category(external, reference),
                "rec5": ext_rec,
                "reference_rec5": ref_rec,
                "delta_rec5": ext_rec - ref_rec,
                "rec_all": ext_all,
                "reference_rec_all": ref_all,
                "delta_rec_all": ext_all - ref_all,
                "answer_diagnosis": external.get("answer_diagnosis"),
                "reference_answer_diagnosis": reference.get("answer_diagnosis"),
                "tool_count": external.get("tool_count"),
                "reference_tool_count": reference.get("tool_count"),
                "turns": external.get("turns"),
                "reference_turns": reference.get("turns"),
                "lsp_route_tool_index": external.get("lsp_route_tool_index"),
                "reference_scheduled_lsp_route": reference.get("scheduled_lsp_route"),
                "reference_scheduled_lsp_route_turn": reference.get(
                    "scheduled_lsp_route_turn"
                ),
                "native_lsp_available": external.get("native_lsp_available"),
                "native_lsp_tool_index": external.get("native_lsp_tool_index"),
                "mcp_cancelled": external.get("mcp_cancelled"),
                "mcp_stderr_internal_errors": external.get(
                    "mcp_stderr_internal_errors"
                ),
            }
        )
    return {
        "comparison_matrix_version": 1,
        "status": "ready",
        "reference_label": reference_label,
        "reference": reference,
        "rows": rows,
    }


def render_comparison_matrix_markdown(matrix: Dict[str, Any]) -> str:
    lines = [
        "# External Agent Comparison Matrix",
        "",
        f"Reference: {matrix.get('reference_label') or 'n/a'}",
        "",
        (
            "| run | gap | rec@5 | d_rec@5 | diag | turns | tools | cm_lsp_idx | "
            "native_lsp | mcp_cancel | stderr_err |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in matrix.get("rows") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("label")),
                    str(row.get("gap_category")),
                    _fmt(row.get("rec5")),
                    _fmt(row.get("delta_rec5")),
                    str(row.get("answer_diagnosis")),
                    _fmt(row.get("turns"), 1),
                    _fmt(row.get("tool_count"), 0),
                    _fmt(row.get("lsp_route_tool_index"), 0),
                    "yes" if row.get("native_lsp_available") else "no",
                    _fmt(row.get("mcp_cancelled"), 0),
                    _fmt(row.get("mcp_stderr_internal_errors"), 0),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _repair_contract_for_gap(gap: str) -> Dict[str, str]:
    return _GAP_REPAIR_CONTRACTS.get(
        gap,
        {
            "repair_action": "inspect_external_comparison_gap",
            "repair_surface": "external_comparison",
            "suggested_prompt_delta": (
                "Inspect the comparison row before choosing an external-agent repair."
            ),
        },
    )


def _gate_blocker_gap(blocker: str) -> str:
    if blocker == "mcp_cancelled_above_gate":
        return "mcp_policy_gap"
    if blocker == "stderr_internal_errors_above_gate":
        return "mcp_transport_noise"
    if blocker == "missing_lsp_route_tool":
        return "tool_adoption_gap"
    if blocker == "missing_native_lsp_surface":
        return "native_lsp_surface_missing"
    if blocker in {"rec5_below_gate", "answer_diagnosis_mismatch"}:
        return "answer_contract_gap"
    return "context_routing_gap"


def build_external_repair_plan(
    *,
    matrix: Optional[Dict[str, Any]],
    gate: Optional[Dict[str, Any]],
    command_manifest: Dict[str, Any],
) -> Dict[str, Any]:
    actions: List[Dict[str, Any]] = []
    if matrix is not None and matrix.get("status") == "ready":
        for row in matrix.get("rows") or []:
            if not isinstance(row, dict):
                continue
            gap = str(row.get("gap_category") or "unknown")
            contract = _repair_contract_for_gap(gap)
            actions.append(
                {
                    "label": row.get("label"),
                    "gap_category": gap,
                    "repair_action": contract["repair_action"],
                    "repair_surface": contract["repair_surface"],
                    "suggested_prompt_delta": contract["suggested_prompt_delta"],
                    "evidence": row,
                }
            )

    if not actions and gate is not None:
        for run in gate.get("runs") or []:
            if not isinstance(run, dict):
                continue
            for blocker in run.get("blockers") or []:
                gap = _gate_blocker_gap(str(blocker))
                contract = _repair_contract_for_gap(gap)
                actions.append(
                    {
                        "label": run.get("label"),
                        "gap_category": gap,
                        "gate_blocker": str(blocker),
                        "repair_action": contract["repair_action"],
                        "repair_surface": contract["repair_surface"],
                        "suggested_prompt_delta": contract["suggested_prompt_delta"],
                        "evidence": run.get("evidence") or {},
                    }
                )

    if not actions and gate is not None and gate.get("promote") is True:
        actions.append(
            {
                "label": None,
                "gap_category": "competitive",
                "repair_action": _GAP_REPAIR_CONTRACTS["competitive"]["repair_action"],
                "repair_surface": _GAP_REPAIR_CONTRACTS["competitive"][
                    "repair_surface"
                ],
                "suggested_prompt_delta": _GAP_REPAIR_CONTRACTS["competitive"][
                    "suggested_prompt_delta"
                ],
                "evidence": {"gate_status": "pass"},
            }
        )

    return {
        "repair_plan_version": 1,
        "status": "ready" if actions else "missing_signal",
        "source_matrix_status": matrix.get("status") if matrix else None,
        "source_gate_status": gate.get("status") if gate else None,
        "rerun_command": command_manifest.get("suggested_command"),
        "actions": actions,
    }


def render_external_repair_plan_markdown(plan: Dict[str, Any]) -> str:
    lines = [
        "# External Agent Repair Plan",
        "",
        "| run | gap | action | surface | prompt_delta |",
        "| --- | --- | --- | --- | --- |",
    ]
    for action in plan.get("actions") or []:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(action.get("label") or "n/a"),
                    str(action.get("gap_category") or "n/a"),
                    str(action.get("repair_action") or ""),
                    str(action.get("repair_surface") or ""),
                    str(action.get("suggested_prompt_delta") or "").replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _safe_label(value: Any) -> str:
    text = str(value or "external").strip().lower()
    chars = [ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text]
    cleaned = "".join(chars).strip("_")
    return cleaned or "external"


def _rerun_requirement(gap: str) -> Tuple[str, str]:
    if gap == "mcp_policy_gap":
        return "needs_policy", "allow_codeminer_mcp_calls"
    if gap == "native_lsp_surface_missing":
        return "needs_environment", "attach_native_ide_lsp_surface"
    if gap == "mcp_transport_noise":
        return "ready_with_stderr_capture", "capture_jsonl_and_stderr"
    return "ready", "capture_jsonl"


def _rerun_output_paths(
    *,
    action: Dict[str, Any],
    command_manifest: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    output_dir = command_manifest.get("output_dir")
    if not output_dir:
        return {"jsonl": None, "stderr": None}
    label = _safe_label(action.get("label"))
    repair_action = _safe_label(action.get("repair_action"))
    base = Path(str(output_dir)) / f"{label}.{repair_action}"
    return {"jsonl": f"{base}.jsonl", "stderr": f"{base}.stderr"}


def build_external_rerun_specs(
    *,
    repair_plan: Dict[str, Any],
    command_manifest: Dict[str, Any],
) -> Dict[str, Any]:
    specs: List[Dict[str, Any]] = []
    for index, action in enumerate(repair_plan.get("actions") or [], start=1):
        if not isinstance(action, dict):
            continue
        gap = str(action.get("gap_category") or "unknown")
        status, requirement = _rerun_requirement(gap)
        output_paths = _rerun_output_paths(
            action=action,
            command_manifest=command_manifest,
        )
        specs.append(
            {
                "index": index,
                "label": action.get("label"),
                "gap_category": gap,
                "repair_action": action.get("repair_action"),
                "repair_surface": action.get("repair_surface"),
                "launcher_status": status,
                "requirement": requirement,
                "prompt_delta": action.get("suggested_prompt_delta"),
                "output_paths": output_paths,
                "validation_command": repair_plan.get("rerun_command")
                or command_manifest.get("suggested_command"),
                "evidence": action.get("evidence") or {},
            }
        )
    return {
        "rerun_specs_version": 1,
        "status": "ready" if specs else "missing_repair_actions",
        "source_repair_plan_status": repair_plan.get("status"),
        "specs": specs,
    }


def render_external_rerun_specs_markdown(specs_payload: Dict[str, Any]) -> str:
    lines = [
        "# External Agent Rerun Specs",
        "",
        "| idx | run | gap | status | requirement | repair | jsonl | validation |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for spec in specs_payload.get("specs") or []:
        output_paths = spec.get("output_paths") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(spec.get("index")),
                    str(spec.get("label") or "n/a"),
                    str(spec.get("gap_category") or "n/a"),
                    str(spec.get("launcher_status") or ""),
                    str(spec.get("requirement") or ""),
                    str(spec.get("repair_action") or ""),
                    str(output_paths.get("jsonl") or "n/a"),
                    str(spec.get("validation_command") or "n/a").replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def evaluate_run_gate(
    rows: List[Dict[str, Any]],
    *,
    labels: Sequence[str],
    min_rec5: float = 1.0,
    require_diagnosis: str = "ok",
    max_mcp_cancelled: int = 0,
    max_stderr_errors: int = 0,
    require_lsp_route: bool = False,
    require_native_lsp: bool = False,
) -> Dict[str, Any]:
    """Evaluate external-agent promotion gates for named runs."""

    by_label = {str(row.get("label")): row for row in rows}
    run_results: List[Dict[str, Any]] = []
    blockers: List[str] = []
    for label in labels:
        row = by_label.get(label)
        if row is None:
            blockers.append(f"{label}:missing_run")
            run_results.append({"label": label, "status": "missing"})
            continue
        run_blockers: List[str] = []
        rec5 = _metric_recall_at_5(row)
        diagnosis = str(row.get("answer_diagnosis") or "")
        mcp_cancelled = int(row.get("mcp_cancelled") or 0)
        stderr_errors = int(row.get("mcp_stderr_internal_errors") or 0)
        if rec5 < min_rec5:
            run_blockers.append("rec5_below_gate")
        if require_diagnosis and diagnosis != require_diagnosis:
            run_blockers.append("answer_diagnosis_mismatch")
        if mcp_cancelled > max_mcp_cancelled:
            run_blockers.append("mcp_cancelled_above_gate")
        if stderr_errors > max_stderr_errors:
            run_blockers.append("stderr_internal_errors_above_gate")
        if require_lsp_route and row.get("lsp_route_tool_index") is None:
            run_blockers.append("missing_lsp_route_tool")
        if require_native_lsp and not row.get("native_lsp_available"):
            run_blockers.append("missing_native_lsp_surface")
        blockers.extend(f"{label}:{blocker}" for blocker in run_blockers)
        run_results.append(
            {
                "label": label,
                "status": "pass" if not run_blockers else "fail",
                "blockers": run_blockers,
                "evidence": {
                    "rec5": rec5,
                    "answer_diagnosis": diagnosis,
                    "mcp_cancelled": mcp_cancelled,
                    "mcp_stderr_internal_errors": stderr_errors,
                    "lsp_route_tool_index": row.get("lsp_route_tool_index"),
                    "native_lsp_available": row.get("native_lsp_available"),
                    "native_lsp_tool_index": row.get("native_lsp_tool_index"),
                },
            }
        )
    return {
        "gate": "external_agent_run_gate",
        "status": "pass" if not blockers else "fail",
        "promote": not blockers,
        "blockers": blockers,
        "criteria": {
            "labels": list(labels),
            "min_rec5": min_rec5,
            "require_diagnosis": require_diagnosis,
            "max_mcp_cancelled": max_mcp_cancelled,
            "max_stderr_errors": max_stderr_errors,
            "require_lsp_route": require_lsp_route,
            "require_native_lsp": require_native_lsp,
        },
        "runs": run_results,
    }


def _parse_run(raw: str) -> Tuple[str, Path]:
    if "=" not in raw:
        path = Path(raw)
        return path.stem, path
    label, path = raw.split("=", 1)
    return label, Path(path)


def _shell_join(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in argv)


def _rerun_command(
    *,
    gt_cell: Path,
    runs: Sequence[Tuple[str, Path]],
    codeminer_cells: Sequence[Tuple[str, Path]],
    reference_label: Optional[str],
    output_dir: Optional[Path],
    repo_path: str,
    require_run_gate: Sequence[str],
    gate_min_rec5: float,
    gate_require_diagnosis: str,
    gate_max_mcp_cancelled: int,
    gate_max_stderr_errors: int,
    gate_require_lsp_route: bool,
    gate_require_native_lsp: bool,
) -> str:
    argv: List[str] = [
        "python",
        "scripts/agent_compile/aggregate_external_agent_baseline.py",
        "--gt-cell",
        str(gt_cell),
    ]
    for label, path in runs:
        argv.extend(["--run", f"{label}={path}"])
    for label, path in codeminer_cells:
        argv.extend(["--codeminer-cell", f"{label}={path}"])
    if reference_label:
        argv.extend(["--reference-label", reference_label])
    if output_dir is not None:
        argv.extend(["--output-dir", str(output_dir)])
    if repo_path:
        argv.extend(["--repo-path", repo_path])
    for label in require_run_gate:
        argv.extend(["--require-run-gate", str(label)])
    if require_run_gate:
        argv.extend(["--gate-min-rec5", str(gate_min_rec5)])
        argv.extend(["--gate-require-diagnosis", str(gate_require_diagnosis)])
        argv.extend(["--gate-max-mcp-cancelled", str(gate_max_mcp_cancelled)])
        argv.extend(["--gate-max-stderr-errors", str(gate_max_stderr_errors)])
        if gate_require_lsp_route:
            argv.append("--gate-require-lsp-route")
        if gate_require_native_lsp:
            argv.append("--gate-require-native-lsp")
    return _shell_join(argv)


def _command_manifest(
    *,
    gt_cell: Path,
    runs: Sequence[Tuple[str, Path]],
    codeminer_cells: Sequence[Tuple[str, Path]],
    reference_label: Optional[str],
    output_dir: Optional[Path],
    repo_path: str,
    require_run_gate: Sequence[str],
    gate_min_rec5: float,
    gate_require_diagnosis: str,
    gate_max_mcp_cancelled: int,
    gate_max_stderr_errors: int,
    gate_require_lsp_route: bool,
    gate_require_native_lsp: bool,
) -> Dict[str, Any]:
    return {
        "command_manifest_version": 1,
        "gt_cell": str(gt_cell),
        "runs": [
            {
                "label": label,
                "path": str(path),
            }
            for label, path in runs
        ],
        "codeminer_cells": [
            {
                "label": label,
                "path": str(path),
            }
            for label, path in codeminer_cells
        ],
        "reference_label": reference_label,
        "output_dir": str(output_dir) if output_dir is not None else None,
        "repo_path": repo_path,
        "gate": {
            "labels": list(require_run_gate),
            "min_rec5": gate_min_rec5,
            "require_diagnosis": gate_require_diagnosis,
            "max_mcp_cancelled": gate_max_mcp_cancelled,
            "max_stderr_errors": gate_max_stderr_errors,
            "require_lsp_route": gate_require_lsp_route,
            "require_native_lsp": gate_require_native_lsp,
        },
        "suggested_command": _rerun_command(
            gt_cell=gt_cell,
            runs=runs,
            codeminer_cells=codeminer_cells,
            reference_label=reference_label,
            output_dir=output_dir,
            repo_path=repo_path,
            require_run_gate=require_run_gate,
            gate_min_rec5=gate_min_rec5,
            gate_require_diagnosis=gate_require_diagnosis,
            gate_max_mcp_cancelled=gate_max_mcp_cancelled,
            gate_max_stderr_errors=gate_max_stderr_errors,
            gate_require_lsp_route=gate_require_lsp_route,
            gate_require_native_lsp=gate_require_native_lsp,
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt-cell", required=True, type=Path)
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument(
        "--codeminer-cell",
        action="append",
        default=[],
        help=(
            "Optional CodeMiner synthesis cell label=path to use as a same-task "
            "comparison reference."
        ),
    )
    parser.add_argument(
        "--reference-label",
        help="Optional CodeMiner cell label to use as the comparison reference.",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repo-path", default="")
    parser.add_argument(
        "--require-run-gate",
        action="append",
        default=[],
        help=(
            "Require a named run label to satisfy the external-agent promotion "
            "gate. Can be repeated. Artifacts are still written first."
        ),
    )
    parser.add_argument("--gate-min-rec5", type=float, default=1.0)
    parser.add_argument("--gate-require-diagnosis", default="ok")
    parser.add_argument("--gate-max-mcp-cancelled", type=int, default=0)
    parser.add_argument("--gate-max-stderr-errors", type=int, default=0)
    parser.add_argument("--gate-require-lsp-route", action="store_true")
    parser.add_argument("--gate-require-native-lsp", action="store_true")
    args = parser.parse_args(argv)

    gt_cell = json.loads(args.gt_cell.read_text())
    gt_blocks = list(gt_cell.get("gt_code_blocks") or [])
    parsed_runs = [_parse_run(raw) for raw in args.run]
    parsed_codeminer_cells = [_parse_run(raw) for raw in args.codeminer_cell]
    rows = [
        score_run(
            label=label,
            path=path,
            gt_blocks=gt_blocks,
            repo_path=args.repo_path,
        )
        for label, path in parsed_runs
    ]
    codeminer_rows = [
        score_codeminer_cell(label=label, path=path)
        for label, path in parsed_codeminer_cells
    ]
    comparison_matrix = None
    if codeminer_rows:
        comparison_matrix = build_comparison_matrix(
            external_rows=rows,
            codeminer_rows=codeminer_rows,
            reference_label=args.reference_label,
        )
    gate = None
    if args.require_run_gate:
        gate = evaluate_run_gate(
            rows,
            labels=args.require_run_gate,
            min_rec5=args.gate_min_rec5,
            require_diagnosis=args.gate_require_diagnosis,
            max_mcp_cancelled=args.gate_max_mcp_cancelled,
            max_stderr_errors=args.gate_max_stderr_errors,
            require_lsp_route=args.gate_require_lsp_route,
            require_native_lsp=args.gate_require_native_lsp,
        )
    payload = {"runs": rows}
    if codeminer_rows:
        payload["codeminer_cells"] = codeminer_rows
    if comparison_matrix is not None:
        payload["comparison_matrix"] = comparison_matrix
    if gate is not None:
        payload["gate"] = gate
    command_manifest = _command_manifest(
        gt_cell=args.gt_cell,
        runs=parsed_runs,
        codeminer_cells=parsed_codeminer_cells,
        reference_label=args.reference_label,
        output_dir=args.output_dir,
        repo_path=args.repo_path,
        require_run_gate=args.require_run_gate,
        gate_min_rec5=args.gate_min_rec5,
        gate_require_diagnosis=args.gate_require_diagnosis,
        gate_max_mcp_cancelled=args.gate_max_mcp_cancelled,
        gate_max_stderr_errors=args.gate_max_stderr_errors,
        gate_require_lsp_route=args.gate_require_lsp_route,
        gate_require_native_lsp=args.gate_require_native_lsp,
    )
    repair_plan = build_external_repair_plan(
        matrix=comparison_matrix,
        gate=gate,
        command_manifest=command_manifest,
    )
    rerun_specs = build_external_rerun_specs(
        repair_plan=repair_plan,
        command_manifest=command_manifest,
    )
    payload["repair_plan"] = repair_plan
    payload["rerun_specs"] = rerun_specs
    markdown = render_markdown(rows)
    comparison_markdown = (
        render_comparison_matrix_markdown(comparison_matrix)
        if comparison_matrix is not None
        else None
    )
    repair_markdown = render_external_repair_plan_markdown(repair_plan)
    rerun_specs_markdown = render_external_rerun_specs_markdown(rerun_specs)
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "external_agent_baseline.json").write_text(
            json.dumps(payload, indent=2)
        )
        (args.output_dir / "external_agent_baseline.md").write_text(markdown)
        if comparison_matrix is not None:
            (args.output_dir / "external_agent_comparison_matrix.json").write_text(
                json.dumps(comparison_matrix, indent=2)
            )
            (args.output_dir / "external_agent_comparison_matrix.md").write_text(
                comparison_markdown or ""
            )
        if gate is not None:
            (args.output_dir / "external_agent_gate.json").write_text(
                json.dumps(gate, indent=2)
            )
        (args.output_dir / "external_agent_command.json").write_text(
            json.dumps(command_manifest, indent=2)
        )
        (args.output_dir / "external_agent_repair_plan.json").write_text(
            json.dumps(repair_plan, indent=2)
        )
        (args.output_dir / "external_agent_repair_plan.md").write_text(repair_markdown)
        (args.output_dir / "external_agent_rerun_specs.json").write_text(
            json.dumps(rerun_specs, indent=2)
        )
        (args.output_dir / "external_agent_rerun_specs.md").write_text(
            rerun_specs_markdown
        )
    print(markdown)
    if comparison_markdown is not None:
        print(comparison_markdown)
    print(repair_markdown)
    print(rerun_specs_markdown)
    if gate is not None and not gate.get("promote"):
        print(
            "external agent gate failed: " + ", ".join(gate.get("blockers") or []),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
