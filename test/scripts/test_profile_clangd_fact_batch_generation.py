# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the durable clangd FactBatch generation profile gate."""

from __future__ import annotations

import pytest

from scripts.profiling.profile_clangd_fact_batch_generation import generation_reuse_gate


def test_generation_reuse_gate_requires_exact_reuse_and_reachability() -> None:
    passed = generation_reuse_gate(
        unit_count=3,
        first_published_units=3,
        reused_units=3,
        published_units=0,
        reachable_object_count=4,
        semantic_match=True,
        graph_materialized=False,
    )
    failed = generation_reuse_gate(
        unit_count=3,
        first_published_units=2,
        reused_units=2,
        published_units=1,
        reachable_object_count=3,
        semantic_match=False,
        graph_materialized=True,
    )

    assert passed == {"passed": True, "failures": []}
    assert failed["passed"] is False
    assert set(failed["failures"]) == {
        "semantic mismatch",
        "clean publication did not write every unit",
        "not every unchanged unit was reused",
        "unchanged publication wrote new units",
        "legacy graph was materialized",
        "catalog reachability does not cover manifest plus units",
    }


@pytest.mark.parametrize(
    "field",
    (
        "unit_count",
        "first_published_units",
        "reused_units",
        "published_units",
        "reachable_object_count",
    ),
)
def test_generation_reuse_gate_rejects_invalid_counts(field: str) -> None:
    arguments = {
        "unit_count": 1,
        "first_published_units": 1,
        "reused_units": 1,
        "published_units": 0,
        "reachable_object_count": 2,
        "semantic_match": True,
        "graph_materialized": False,
    }
    arguments[field] = -1
    with pytest.raises(ValueError, match=field):
        generation_reuse_gate(**arguments)


def test_generation_reuse_gate_rejects_vacuous_empty_generation() -> None:
    result = generation_reuse_gate(
        unit_count=0,
        first_published_units=0,
        reused_units=0,
        published_units=0,
        reachable_object_count=1,
        semantic_match=True,
        graph_materialized=False,
    )

    assert result["passed"] is False
    assert result["failures"] == ["no FactBatch units were produced"]
