# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for reusable per-query sweep helpers."""

from __future__ import annotations

from codeminer.eval.agent_runner.query_sweep import (
    filter_query_rows,
    group_query_rows_by_instance,
    language_key_for_query_row,
    query_targets,
)


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


def test_language_key_for_query_row_prefers_explicit_then_language_group():
    assert language_key_for_query_row({"language_key": "ruby"}) == "ruby"
    assert language_key_for_query_row({"language_group": "TypeScript/JavaScript"}) == (
        "typescript"
    )
    assert language_key_for_query_row({"source_config": "Python"}) == "python"
