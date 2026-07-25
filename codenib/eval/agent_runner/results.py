# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent result observation helpers for evaluation harnesses."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from .trace_summary import summarize_agent_trace


def summarize_agent_result(result: Any) -> Dict[str, Any]:
    """Return evaluation-friendly observations from an ``AgentResult``.

    This helper records what happened during a run without rewriting answers or
    applying scorer-specific normalization. Callers can combine the output with
    experiment-only fields such as pre-load candidates or verification status.
    """
    nodes: List[Any] = []
    tool_calls: List[Dict[str, Any]] = []
    file_read_paths: List[str] = []
    file_reads: List[Dict[str, Any]] = []
    raw_tool_calls = list(getattr(result, "tool_calls", []) or [])

    for tool_call in raw_tool_calls:
        raw_result = getattr(tool_call, "result", None)
        is_list_result = isinstance(raw_result, list)
        tool_calls.append(
            {
                "skill_id": getattr(tool_call, "skill_id", None),
                "n_results": len(raw_result) if is_list_result else 0,
                "error": bool(getattr(tool_call, "error", None)),
            }
        )
        if is_list_result:
            nodes.extend(raw_result)

        if getattr(tool_call, "skill_id", None) != "read":
            continue
        if getattr(tool_call, "error", None):
            continue
        args = getattr(tool_call, "arguments", None) or {}
        if not isinstance(args, Mapping):
            continue
        path = args.get("file_path")
        if not path:
            continue
        file_read_paths.append(str(path))
        file_reads.append(
            {
                "file_path": str(path),
                "offset": args.get("offset"),
                "limit": args.get("limit"),
            }
        )

    return {
        "nodes": nodes,
        "tool_calls": tool_calls,
        "file_read_paths": file_read_paths,
        "file_reads": file_reads,
        "trace_summary": summarize_agent_trace(result).to_dict(),
        "answer": getattr(result, "answer", None) or "",
        "total_turns": getattr(result, "total_turns", None),
        "total_duration_ms": getattr(result, "total_duration_ms", None),
        "tool_call_count": len(raw_tool_calls),
    }
