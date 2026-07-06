# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for paired LSP-route latency replay."""

from __future__ import annotations

from codeminer.agent.route_context import fingerprint_lsp_route_nodes
from codeminer.eval.agent_runner.lsp_latency import (
    render_lsp_route_latency_markdown,
    replay_lsp_route_latency,
)
from codeminer.types import QueriedNode


def _node():
    return QueriedNode(
        node_name="pkg.HandleRequest",
        type="function",
        file="pkg/service.py",
        start_line=4,
        end_line=8,
        content="route endpoint: direct seed HandleRequest",
    )


def test_replay_lsp_route_latency_replays_dynamic_args_against_same_backend():
    node = _node()
    route_args = {
        "symbols": ["HandleRequest"],
        "query": "fix default config",
        "top_k": 5,
        "include_neighbors": True,
    }
    cells = [
        {
            "instance_id": "demo__repo-1",
            "subset_id": "lsp_route_preload",
            "success": True,
            "trace_summary": {
                "lsp_route_context": {
                    "status": "offered",
                    "duration_ms": 4.0,
                    "route_args": route_args,
                    "route_fingerprint": fingerprint_lsp_route_nodes([node]),
                }
            },
        },
        {
            "instance_id": "demo__repo-1",
            "subset_id": "lsp_route_skill",
            "success": True,
            "trace_summary": {
                "lsp_route_tool_calls": [
                    {
                        "turn": 1,
                        "duration_ms": 3.0,
                        "model_can_use_turn": 2,
                        "model_can_use_ms": 103.0,
                        "extra_model_round_trips": 1,
                        "result_count": 1,
                        "route_args": route_args,
                        "route_fingerprint": fingerprint_lsp_route_nodes([node]),
                    }
                ]
            },
        },
    ]

    seen = []

    def graph_loader(prebuilt_root, instance_id):
        seen.append((prebuilt_root, instance_id))
        return object()

    def route_fn(graph, **kwargs):
        seen.append(kwargs)
        return [node]

    rows = replay_lsp_route_latency(
        cells,
        prebuilt_root="/prebuilt",
        graph_loader=graph_loader,
        route_fn=route_fn,
    )

    assert len(rows) == 1
    row = rows[0]
    assert seen[0] == ("/prebuilt", "demo__repo-1")
    assert seen[1] == route_args
    assert row["same_route_result"] is True
    assert row["observed_preload_same_args"] is True
    assert row["observed_preload_same_hash"] is True
    assert row["dynamic_model_can_use_turn"] == 2
    assert row["extra_model_round_trips"] == 1
    assert row["route_visible_ms_saved"] <= 103.0

    markdown = render_lsp_route_latency_markdown(rows)
    assert "# LSP-route same-backend latency replay" in markdown
    assert "| demo__repo-1 | lsp_route_skill | yes |" in markdown
