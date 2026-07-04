# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for synthesis feedback-probe row selection."""

from __future__ import annotations

from scripts.agent_compile.run_synthesis_sweep import _select_rows


def _row(instance: str, category: str, n: int):
    return {
        "instance_id": instance,
        "category": category,
        "query_id": f"{instance}_{category}_{n}",
    }


def test_select_rows_caps_each_instance_category_bucket():
    rows = [
        _row("repo-a", "behavioral", 1),
        _row("repo-a", "behavioral", 2),
        _row("repo-a", "symbol_hint", 1),
        _row("repo-a", "symbol_hint", 2),
        _row("repo-b", "behavioral", 1),
        _row("repo-b", "behavioral", 2),
        _row("repo-b", "traversal", 1),
        _row("repo-b", "traversal", 2),
    ]

    selected = _select_rows(
        rows,
        instances=["repo-a", "repo-b"],
        categories={"behavioral", "traversal"},
        max_queries_per_category=1,
    )

    assert [r["query_id"] for r in selected] == [
        "repo-a_behavioral_1",
        "repo-b_behavioral_1",
        "repo-b_traversal_1",
    ]


def test_select_rows_legacy_max_queries_caps_each_instance():
    rows = [
        _row("repo-a", "behavioral", 1),
        _row("repo-a", "behavioral", 2),
        _row("repo-a", "symbol_hint", 1),
        _row("repo-b", "behavioral", 1),
        _row("repo-b", "symbol_hint", 1),
    ]

    selected = _select_rows(rows, max_queries=2)

    assert [r["query_id"] for r in selected] == [
        "repo-a_behavioral_1",
        "repo-a_behavioral_2",
        "repo-b_behavioral_1",
        "repo-b_symbol_hint_1",
    ]
