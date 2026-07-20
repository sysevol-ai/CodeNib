# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for reusable per-query sweep helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeminer.eval.agent_runner.query_sweep import (
    filter_query_rows,
    group_query_rows_by_instance,
    language_key_for_query_row,
    limit_instance_query_rows,
    query_targets,
    run_query_sweep,
    select_query_rows,
)
from codeminer.eval.agent_runner.sweep_config import SweepConfig


def test_sweep_config_rejects_non_json_run_metadata(tmp_path):
    config = tmp_path / "cfg.yaml"
    config.write_text(
        """
sweep_id: invalid_metadata
model: test/model
embedding_model: test/embedding
subsets: {defaults: []}
run_metadata:
  unquoted_date: 2026-07-20
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="JSON-serializable"):
        SweepConfig.from_yaml(config)


def test_query_targets_normalizes_files_and_simplified_symbols():
    files, symbols = query_targets(
        {
            "gt_files": ["/tmp/repo/src/app.py", ""],
            "gt_symbols": ["src/app.py:App", "src/app.py:App.run()"],
        },
        simplified_symbols=True,
    )

    assert files == ["tmp/repo/src/app.py"]
    assert symbols == ["src/app.py:App.run()"]


def test_query_targets_can_keep_non_function_symbols():
    _, symbols = query_targets(
        {"gt_symbols": ["src/app.py:App", "src/app.py:App.run()"]},
        simplified_symbols=False,
    )

    assert symbols == ["src/app.py:App", "src/app.py:App.run()"]


def test_filter_query_rows_restricts_categories_without_mutating_inputs():
    row = {"instance_id": "i1", "category": "behavioral"}
    other = {"instance_id": "i2", "category": "symbol_hint"}

    selected = filter_query_rows([row, other], {"behavioral"})
    selected[0]["category"] = "changed"

    assert [item["instance_id"] for item in selected] == ["i1"]
    assert row["category"] == "behavioral"


def test_select_query_rows_applies_instance_allowlist_and_cap_in_order():
    rows = [
        {"instance_id": "i1", "query_id": "q1", "category": "behavioral"},
        {"instance_id": "i1", "query_id": "q2", "category": "behavioral"},
        {"instance_id": "i2", "query_id": "q3", "category": "behavioral"},
        {"instance_id": "i3", "query_id": "q4", "category": "behavioral"},
    ]

    selected = select_query_rows(
        rows,
        instances=["i3", "i1", "missing"],
        max_instances=1,
    )

    assert [row["query_id"] for row in selected] == ["q1", "q2"]


def test_select_query_rows_caps_instances_after_category_filter():
    rows = [
        {"instance_id": "i1", "query_id": "q1", "category": "symbol_hint"},
        {"instance_id": "i2", "query_id": "q2", "category": "behavioral"},
        {"instance_id": "i2", "query_id": "q3", "category": "behavioral"},
        {"instance_id": "i3", "query_id": "q4", "category": "behavioral"},
    ]

    selected = select_query_rows(
        rows,
        categories={"behavioral"},
        max_instances=1,
    )

    assert [row["query_id"] for row in selected] == ["q2", "q3"]


def test_group_query_rows_by_instance_preserves_order_and_skips_missing_ids():
    grouped = group_query_rows_by_instance(
        [
            {"instance_id": "i1", "query_id": "q1"},
            {"instance_id": "", "query_id": "missing"},
            {"instance_id": "i1", "query_id": "q2"},
            {"instance_id": "i2", "query_id": "q3"},
        ]
    )

    assert [row["query_id"] for row in grouped["i1"]] == ["q1", "q2"]
    assert [row["query_id"] for row in grouped["i2"]] == ["q3"]


def test_limit_instance_query_rows_round_robins_categories_with_offset():
    rows = [
        {"query_id": "b1", "category": "behavioral"},
        {"query_id": "b2", "category": "behavioral"},
        {"query_id": "f1", "category": "file_hint"},
        {"query_id": "f2", "category": "file_hint"},
        {"query_id": "s1", "category": "symbol_hint"},
    ]

    first = limit_instance_query_rows(
        rows, 4, strategy="category_round_robin", category_offset=0
    )
    shifted = limit_instance_query_rows(
        rows, 4, strategy="category_round_robin", category_offset=1
    )

    assert [row["query_id"] for row in first] == ["b1", "f1", "s1", "b2"]
    assert [row["query_id"] for row in shifted] == ["f1", "s1", "b1", "f2"]


def test_limit_instance_query_rows_preserves_dataset_order_by_default():
    rows = [
        {"query_id": "b1", "category": "behavioral"},
        {"query_id": "b2", "category": "behavioral"},
        {"query_id": "f1", "category": "file_hint"},
    ]

    selected = limit_instance_query_rows(rows, 2)

    assert [row["query_id"] for row in selected] == ["b1", "b2"]


def test_language_key_for_query_row_prefers_explicit_then_language_group():
    assert language_key_for_query_row({"language_key": "ruby"}) == "ruby"
    assert language_key_for_query_row({"language_group": "TypeScript/JavaScript"}) == (
        "typescript"
    )
    assert language_key_for_query_row({"source_config": "Python"}) == "python"


def test_run_query_sweep_stages_only_declared_indexes(monkeypatch, tmp_path):
    calls = {}

    def has_required(prebuilt_dir, instance_id, embedding_model, index_types):
        calls["preflight"] = (
            prebuilt_dir,
            instance_id,
            embedding_model,
            set(index_types),
        )
        return True

    def stage(prebuilt_dir, instance_id, cache_dir, *, required_index_types):
        calls["stage"] = (
            prebuilt_dir,
            instance_id,
            cache_dir,
            set(required_index_types),
        )
        return cache_dir

    def unexpected_symbol_graph(*_args, **_kwargs):
        raise AssertionError("vector-only sweeps must not load the symbol graph")

    monkeypatch.setattr(
        "codeminer.eval.agent_runner.prebuilt.has_required_indexes", has_required
    )
    monkeypatch.setattr(
        "codeminer.eval.agent_runner.prebuilt.repo_path_for",
        lambda _root, _instance: "/snapshots/repo",
    )
    monkeypatch.setattr(
        "codeminer.eval.agent_runner.prebuilt.stage_prebuilt_indexes", stage
    )
    monkeypatch.setattr(
        "codeminer.eval.agent_runner.sweep.load_full_contexts",
        lambda _cfg, _repo, _cache: {},
    )
    monkeypatch.setattr(
        "codeminer.eval.agent_runner.sweep.run_cell",
        lambda *_args, **_kwargs: {
            "answer": "",
            "file_read_paths": [],
            "nodes": [],
            "preload_candidates": [],
            "verify_triggered": False,
            "verify_resolved": False,
            "tool_calls": [],
            "trace_summary": {},
            "total_turns": 1,
            "total_tokens": 1,
            "cost_usd": 0.0,
            "cache_read_input_tokens": 0,
        },
    )
    monkeypatch.setattr(
        "codeminer.eval.agent_runner.symbols.build_prebuilt_symbol_span_index",
        unexpected_symbol_graph,
    )
    monkeypatch.setattr(
        "codeminer.eval.agent_runner.scoring.evaluate_agent_localization",
        lambda **_kwargs: SimpleNamespace(
            metrics={"answer_blocks": {5: {"recall": 0.0}}},
            to_record_fields=lambda: {},
        ),
    )
    monkeypatch.setattr(
        "codeminer.eval.retrieval_eval.collect_target_blocks", lambda _row: []
    )
    monkeypatch.setattr(
        "codeminer.llm.litellm_chat.LiteLLMChat", lambda **_kwargs: object()
    )

    cfg = SweepConfig(
        sweep_id="vector-only",
        subsets={"vector": ["embedding_search"]},
        model="provider/model",
        embedding_model="org/embed-small",
        prebuilt_dir=str(tmp_path / "prebuilt"),
        reps=1,
    )
    output_dir = tmp_path / "results"

    summary = run_query_sweep(
        cfg,
        output_dir,
        [
            {
                "instance_id": "org__repo-1",
                "query_id": "query-1",
                "query": "Locate the implementation",
                "gt_files": [],
                "gt_symbols": [],
            }
        ],
        resume=False,
    )

    assert summary["required_index_types"] == ["vector"]
    assert summary["protocol"]["model"] == "provider/model"
    assert summary["protocol"]["embedding_model"] == "org/embed-small"
    assert summary["protocol"]["query_selection"] == "dataset_order"
    assert summary["cached"] == []
    assert calls["preflight"] == (
        str(tmp_path / "prebuilt"),
        "org__repo-1",
        "org/embed-small",
        {"vector"},
    )
    assert calls["stage"] == (
        str(tmp_path / "prebuilt"),
        "org__repo-1",
        str(output_dir / "cache" / "org__repo-1"),
        {"vector"},
    )

    resumed = run_query_sweep(
        cfg,
        output_dir,
        [
            {
                "instance_id": "org__repo-1",
                "query_id": "query-1",
                "query": "Locate the implementation",
                "gt_files": [],
                "gt_symbols": [],
            }
        ],
        resume=True,
    )

    assert resumed["completed"] == []
    assert resumed["cached"] == ["query-1__vector__rep1"]
