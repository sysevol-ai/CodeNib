# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Paired same-backend latency replay for graph-backed LSP routes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from codeminer.agent.lsp_graph import lsp_route
from codeminer.agent.route_context import (
    canonical_lsp_route_args,
    fingerprint_lsp_route_nodes,
)

from .prebuilt import load_prebuilt_code_graph

GraphLoader = Callable[[str, str], Any]
RouteFn = Callable[..., Sequence[Any]]


def load_cell_records(cells_dir: str | Path) -> List[Dict[str, Any]]:
    """Load successful sweep cell records from a directory."""

    root = Path(cells_dir)
    cells: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        with path.open("r", encoding="utf-8") as fh:
            cell = json.load(fh)
        if cell.get("success"):
            cells.append(cell)
    return cells


def replay_lsp_route_latency(
    cells: Sequence[Mapping[str, Any]],
    *,
    prebuilt_root: str,
    graph_loader: GraphLoader = load_prebuilt_code_graph,
    route_fn: RouteFn = lsp_route,
) -> List[Dict[str, Any]]:
    """Replay dynamic ``lsp_route`` calls directly against the same backend.

    The replay isolates route exposure latency from route-argument selection:
    for each dynamic tool call, it runs the exact canonical ``route_args`` again
    on the prebuilt symbol graph and compares that backend-only duration with
    the dynamic path's model-visible timestamp.
    """

    preload_by_instance = _preload_context_by_instance(cells)
    graph_cache: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    for cell in cells:
        instance_id = str(cell.get("instance_id") or "")
        if not instance_id:
            continue
        calls = _lsp_route_tool_calls(cell)
        if not calls:
            continue
        for call_index, call in enumerate(calls, 1):
            row = _replay_call(
                cell,
                call,
                call_index=call_index,
                prebuilt_root=prebuilt_root,
                graph_cache=graph_cache,
                graph_loader=graph_loader,
                route_fn=route_fn,
                preload_context=preload_by_instance.get(instance_id),
            )
            rows.append(row)
    return rows


def render_lsp_route_latency_markdown(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render paired latency replay rows as a compact markdown report."""

    lines = ["# LSP-route same-backend latency replay", ""]
    lines.append(
        "Each row replays a dynamic `lsp_route` tool call's canonical args "
        "directly against the same prebuilt graph backend. `saved ms` is the "
        "dynamic model-visible route time minus replay backend time."
    )
    lines.append("")
    head = [
        "instance",
        "arm",
        "same route",
        "dyn backend ms",
        "replay backend ms",
        "dyn visible ms",
        "saved ms",
        "model use turn",
        "extra trips",
        "dyn hash",
        "replay hash",
        "preload same args",
        "preload same hash",
    ]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("| " + " | ".join("---" for _ in head) + " |")
    for row in rows:
        rendered = [
            str(row.get("instance_id") or ""),
            str(row.get("arm") or ""),
            _yes_no(row.get("same_route_result")),
            _fmt(row.get("dynamic_backend_duration_ms"), 1),
            _fmt(row.get("replay_backend_duration_ms"), 1),
            _fmt(row.get("dynamic_model_can_use_ms"), 1),
            _fmt(row.get("route_visible_ms_saved"), 1),
            _fmt(row.get("dynamic_model_can_use_turn"), 1),
            _fmt(row.get("extra_model_round_trips"), 1),
            str(row.get("dynamic_route_fingerprint") or "n/a")[:12],
            str(row.get("replay_route_fingerprint") or "n/a")[:12],
            _yes_no(row.get("observed_preload_same_args")),
            _yes_no(row.get("observed_preload_same_hash")),
        ]
        lines.append("| " + " | ".join(rendered) + " |")
    lines.append("")
    return "\n".join(lines)


def write_lsp_route_latency_report(
    *,
    cells_dir: str | Path,
    output_dir: str | Path,
    prebuilt_root: str,
) -> str:
    """Replay dynamic route calls and write markdown + JSON artifacts."""

    cells = load_cell_records(cells_dir)
    rows = replay_lsp_route_latency(cells, prebuilt_root=prebuilt_root)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    markdown = render_lsp_route_latency_markdown(rows)
    (out / "lsp_latency_replay.md").write_text(markdown, encoding="utf-8")
    (out / "lsp_latency_replay.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return markdown


def _replay_call(
    cell: Mapping[str, Any],
    call: Mapping[str, Any],
    *,
    call_index: int,
    prebuilt_root: str,
    graph_cache: Dict[str, Any],
    graph_loader: GraphLoader,
    route_fn: RouteFn,
    preload_context: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    instance_id = str(cell.get("instance_id") or "")
    raw_args = call.get("route_args") or call.get("arguments") or {}
    route_args = _canonical_args(raw_args)
    row: Dict[str, Any] = {
        "instance_id": instance_id,
        "arm": cell.get("subset_id"),
        "call_index": call_index,
        "dynamic_turn": call.get("turn"),
        "dynamic_backend_duration_ms": call.get("duration_ms"),
        "dynamic_model_can_use_turn": call.get("model_can_use_turn"),
        "dynamic_model_can_use_ms": call.get("model_can_use_ms"),
        "extra_model_round_trips": call.get("extra_model_round_trips"),
        "dynamic_route_result_count": call.get("result_count"),
        "dynamic_route_fingerprint": call.get("route_fingerprint"),
        "route_args": route_args,
    }

    try:
        graph = graph_cache.get(instance_id)
        if graph is None:
            graph = graph_loader(prebuilt_root, instance_id)
            graph_cache[instance_id] = graph
        start = time.monotonic()
        nodes = list(route_fn(graph, **route_args) or [])
        replay_duration_ms = (time.monotonic() - start) * 1000
        replay_hash = fingerprint_lsp_route_nodes(nodes)
        row.update(
            {
                "replay_backend_duration_ms": replay_duration_ms,
                "replay_route_result_count": len(nodes),
                "replay_route_fingerprint": replay_hash,
                "same_route_result": replay_hash == call.get("route_fingerprint"),
            }
        )
    except Exception as exc:  # noqa: BLE001 - report the failed replay row.
        row.update({"replay_error": f"{type(exc).__name__}: {exc}"})

    visible_ms = row.get("dynamic_model_can_use_ms")
    replay_ms = row.get("replay_backend_duration_ms")
    if isinstance(visible_ms, (int, float)) and isinstance(replay_ms, (int, float)):
        row["route_visible_ms_saved"] = float(visible_ms) - float(replay_ms)

    if preload_context:
        preload_args = _canonical_args(preload_context.get("route_args") or {})
        preload_hash = preload_context.get("route_fingerprint")
        row.update(
            {
                "observed_preload_route_args": preload_args,
                "observed_preload_route_fingerprint": preload_hash,
                "observed_preload_duration_ms": preload_context.get("duration_ms"),
                "observed_preload_same_args": preload_args == route_args,
                "observed_preload_same_hash": preload_hash
                == call.get("route_fingerprint"),
            }
        )
    return row


def _preload_context_by_instance(
    cells: Sequence[Mapping[str, Any]],
) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for cell in cells:
        summary = _trace_summary(cell).get("lsp_route_context") or {}
        if not isinstance(summary, Mapping) or summary.get("status") != "offered":
            continue
        instance_id = str(cell.get("instance_id") or "")
        if instance_id and instance_id not in out:
            out[instance_id] = summary
    return out


def _lsp_route_tool_calls(cell: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    calls = _trace_summary(cell).get("lsp_route_tool_calls") or []
    return [call for call in calls if isinstance(call, Mapping)]


def _trace_summary(cell: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = cell.get("trace_summary") or {}
    return summary if isinstance(summary, Mapping) else {}


def _canonical_args(args: Mapping[str, Any]) -> Dict[str, Any]:
    return canonical_lsp_route_args(
        symbols=args.get("symbols") or [],
        query=args.get("query"),
        top_k=args.get("top_k", 12),
        include_neighbors=args.get("include_neighbors", True),
    )


def _fmt(value: Any, digits: int) -> str:
    return f"{float(value):.{digits}f}" if isinstance(value, (int, float)) else "n/a"


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "n/a"


__all__ = [
    "load_cell_records",
    "render_lsp_route_latency_markdown",
    "replay_lsp_route_latency",
    "write_lsp_route_latency_report",
]
