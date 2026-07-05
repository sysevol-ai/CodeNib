# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for reusable agent-runner sweep helpers."""

from __future__ import annotations

import pytest

from codeminer.agent.agent_types import AgentResult
from codeminer.eval.agent_runner.sweep import (
    all_index_skill_ids,
    lsp_route_context_for_subset,
    run_cell,
    validate_sweep_harness,
)
from codeminer.eval.agent_runner.sweep_config import SweepConfig


def test_validate_sweep_harness_accepts_registered_tools_and_skills():
    cfg = SweepConfig(
        sweep_id="ok",
        default_tool_ids=["read", "grep"],
        subsets={"retrieval": ["read", "bm25_search"]},
    )

    validate_sweep_harness(cfg)


def test_validate_sweep_harness_rejects_unknown_default_tool():
    cfg = SweepConfig(
        sweep_id="bad_defaults",
        default_tool_ids=["file_read"],
        subsets={"baseline": ["read"]},
    )

    with pytest.raises(ValueError, match="default_tool_ids"):
        validate_sweep_harness(cfg)


def test_validate_sweep_harness_rejects_unknown_skill():
    cfg = SweepConfig(
        sweep_id="bad_skill",
        subsets={"retrieval": ["definitely_not_a_skill"]},
    )

    with pytest.raises(ValueError, match="unknown skills"):
        validate_sweep_harness(cfg)


def test_validate_sweep_harness_rejects_unknown_lsp_route_context_arm():
    cfg = SweepConfig(
        sweep_id="bad_route_arm",
        subsets={"grep_only": ["read", "grep"]},
        lsp_route_context={"missing": {}},
    )

    with pytest.raises(ValueError, match="unknown subset"):
        validate_sweep_harness(cfg)


def test_lsp_route_context_loads_graph_skill_without_exposing_it_to_arm():
    cfg = SweepConfig(
        sweep_id="route",
        subsets={
            "grep_only": ["read", "grep", "glob", "bash"],
            "route_context": ["read", "grep", "glob", "bash"],
        },
        lsp_route_context={"route_context": {"top_k": 8}},
    )

    assert "lsp_route" in all_index_skill_ids(cfg)
    assert "lsp_route" not in cfg.subsets["route_context"]
    assert lsp_route_context_for_subset(cfg, "grep_only") is None
    assert lsp_route_context_for_subset(cfg, "route_context") == {"top_k": 8}


def test_run_cell_passes_lsp_route_context_options_to_harness(monkeypatch, tmp_path):
    captured = []

    class FakeRunner:
        def run(self, prompt):
            return AgentResult(answer=f"done: {prompt}", total_turns=1)

    from codeminer.agent.harness import AgentHarnessSpec

    def fake_create_runner(self, **kwargs):
        captured.append(self)
        return FakeRunner()

    monkeypatch.setattr(AgentHarnessSpec, "create_runner", fake_create_runner)
    cfg = SweepConfig(
        sweep_id="route",
        subsets={"graph": ["lsp_route"]},
        lsp_route_context={
            "graph": {
                "seed_limit": 4,
                "seed_policy": "specific",
                "top_k": 9,
                "include_neighbors": False,
            }
        },
        force_localization_contract=False,
    )

    out = run_cell(
        cfg,
        contexts={},
        llm=object(),
        repo_path=str(tmp_path),
        language_key="python",
        query="Fix `HandleRequest`",
        subset_id="graph",
        skills=["lsp_route"],
    )

    assert out["answer"] == "done: Fix `HandleRequest`"
    assert out["trace_summary"]["schema_version"] is None
    assert captured
    spec = captured[0]
    assert spec.enable_lsp_route_context is True
    assert spec.lsp_route_seed_limit == 4
    assert spec.lsp_route_seed_policy == "specific"
    assert spec.lsp_route_top_k == 9
    assert spec.lsp_route_include_neighbors is False
