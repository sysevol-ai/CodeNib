# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for graph-backed LSP baseline adapters."""

from __future__ import annotations

from codeminer.eval.agent_runner.baseline import BaselineTask
from codeminer.eval.agent_runner.lsp_baseline import (
    LSPRouteBaselineConfig,
    build_lsp_route_result_entry,
    extract_lsp_symbol_seeds,
    locations_from_lsp_nodes,
    run_lsp_route_baseline,
)
from codeminer.types import QueriedNode


class _RouteGraph:
    def __init__(self):
        self.name_to_vertex = {
            "svc.HandleRequest": 0,
            "svc.NewResolver": 1,
            "svc.DefaultConfig": 2,
        }
        self._attr = {
            0: {
                "name": "svc.HandleRequest",
                "unified_name": "service.py:HandleRequest()",
                "type": "function",
                "file": "service.py",
                "start_line": 19,
                "end_line": 49,
            },
            1: {
                "name": "svc.NewResolver",
                "unified_name": "resolver.py:NewResolver()",
                "type": "function",
                "file": "resolver.py",
                "start_line": 4,
                "end_line": 14,
            },
            2: {
                "name": "svc.DefaultConfig",
                "unified_name": "config.py:DefaultConfig()",
                "type": "function",
                "file": "config.py",
                "start_line": 1,
                "end_line": 7,
            },
        }
        self._succ = {
            "svc.HandleRequest": [1],
            "svc.NewResolver": [2],
        }
        self._pred = {}

    def get_node_info_by_name(self, name):
        return self._attr.get(self.name_to_vertex.get(name))

    def get_node_info_by_id(self, vertex_id):
        return self._attr.get(vertex_id)

    def get_successors(self, name):
        return self._succ.get(name, [])

    def get_predecessors(self, name):
        return self._pred.get(name, [])


def test_extract_lsp_symbol_seeds_uses_explicit_context_before_query():
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix `HandleRequest` when HTTP2Mode is enabled",
        repo_path="/repo",
        context={
            "symbol_seeds": ["NewResolver", "HandleRequest"],
            "issue_body": "HTTP2Mode fails in `HandleRequest`",
        },
        target_symbols=("service.py:GroundTruthOnly()",),
    )

    assert extract_lsp_symbol_seeds(task, limit=4) == [
        "NewResolver",
        "HandleRequest",
        "HTTP2Mode",
    ]


def test_locations_from_lsp_nodes_converts_lines_and_unified_names():
    locations = locations_from_lsp_nodes(
        [
            QueriedNode(
                node_name="service.py:HandleRequest()",
                type="function",
                file="service.py",
                start_line=19,
                end_line=49,
                content="route endpoint: direct seed HandleRequest",
            )
        ]
    )

    assert [location.to_dict() for location in locations] == [
        {
            "name": "HandleRequest()",
            "type": "function",
            "file_path": "service.py",
            "line_start": 20,
            "line_end": 50,
            "action": "modify",
            "description": "route endpoint: direct seed HandleRequest",
        }
    ]


def test_lsp_route_baseline_returns_locations_and_usage():
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix HandleRequest resolver default config",
        repo_path="/repo",
        context={"symbol_seeds": ["HandleRequest", "NewResolver"]},
        target_files=("service.py",),
        target_symbols=("service.py:HandleRequest()",),
    )

    result = run_lsp_route_baseline(
        task=task,
        graph=_RouteGraph(),
        config=LSPRouteBaselineConfig(top_k=3),
    )

    assert result.success is True
    assert [location.symbol_id() for location in result.locations] == [
        "service.py:HandleRequest()",
        "resolver.py:NewResolver()",
        "config.py:DefaultConfig()",
    ]
    assert result.usage == {
        "backend": "static_graph_lsp_route",
        "top_k": 3,
        "seed_limit": 8,
        "include_neighbors": True,
        "symbol_seeds": ["HandleRequest", "NewResolver"],
        "route_count": 3,
    }


def test_build_lsp_route_result_entry_scores_with_baseline_envelope():
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix HandleRequest resolver default config",
        repo_path="/repo",
        context={"symbol_seeds": ["HandleRequest", "NewResolver"]},
        target_files=("service.py",),
        target_symbols=("service.py:HandleRequest()",),
    )

    entry = build_lsp_route_result_entry(
        task=task,
        graph=_RouteGraph(),
        metrics_k=[1, 3],
        config=LSPRouteBaselineConfig(top_k=3),
    )

    assert entry["agent"] == "lsp_route"
    assert entry["model"] is None
    assert entry["metric_k_files"] == ["service.py", "resolver.py", "config.py"]
    assert entry["metric_k_node_ids"] == [
        "service.py:HandleRequest()",
        "resolver.py:NewResolver()",
        "config.py:DefaultConfig()",
    ]
    assert entry["metrics"]["files"][1]["accuracy"] == 1.0
    assert entry["metrics"]["symbols"][1]["accuracy"] == 1.0


def test_lsp_route_baseline_without_seeds_is_successful_empty_result():
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix the resolver behavior",
        repo_path="/repo",
        target_files=("service.py",),
        target_symbols=("service.py:HandleRequest()",),
    )

    result = run_lsp_route_baseline(task=task, graph=_RouteGraph())

    assert result.success is True
    assert result.locations == ()
    assert result.usage["symbol_seeds"] == []
    assert result.usage["route_count"] == 0


def test_lsp_route_baseline_records_missing_graph_as_failure():
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix `HandleRequest`",
        repo_path="/repo",
    )

    result = run_lsp_route_baseline(task=task, graph=None)

    assert result.success is False
    assert result.error_message == "symbol graph unavailable"
