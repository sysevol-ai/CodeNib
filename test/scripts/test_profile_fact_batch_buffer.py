# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the FactBatchBuffer promotion gate."""

from __future__ import annotations

import gc

import pytest

from scripts.profiling.profile_fact_batch_buffer import (
    _timed,
    acceleration_decision,
    summarize_samples,
)


def test_summarize_samples_reports_nearest_rank_p95():
    summary = summarize_samples([4.0, 1.0, 3.0, 2.0])

    assert summary == {
        "samples": 4,
        "min_seconds": 1.0,
        "median_seconds": 2.5,
        "p95_seconds": 4.0,
        "max_seconds": 4.0,
    }


def test_timed_disables_and_restores_cyclic_gc_on_failure():
    was_enabled = gc.isenabled()
    gc.enable()

    def fail():
        assert gc.isenabled() is False
        raise RuntimeError("stop")

    try:
        with pytest.raises(RuntimeError, match="stop"):
            _timed(fail)
        assert gc.isenabled() is True
    finally:
        if not was_enabled:
            gc.disable()


def test_timed_preserves_an_already_disabled_gc_state():
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        observed, elapsed = _timed(gc.isenabled)
        assert observed is False
        assert elapsed >= 0
        assert gc.isenabled() is False
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("values", [[], [1.0, -0.1], [float("nan")], [float("inf")]])
def test_summarize_samples_rejects_invalid_values(values):
    with pytest.raises(ValueError):
        summarize_samples(values)


def test_acceleration_decision_promotes_local_path_at_threshold():
    decision = acceleration_decision(
        legacy_local_seconds=1.0,
        candidate_local_seconds=0.75,
        threshold=0.20,
    )

    assert decision["gate"] == "local_decode_end_to_end"
    assert decision["gate_improvement_fraction"] == pytest.approx(0.25)
    assert decision["promoted"] is True
    assert decision["recommendation"] == "promote-fact-buffer"


def test_acceleration_decision_applies_cold_start_amdahl_gate():
    decision = acceleration_decision(
        legacy_local_seconds=1.0,
        candidate_local_seconds=0.5,
        external_index_seconds=9.0,
        threshold=0.20,
    )

    assert decision["local_improvement_fraction"] == pytest.approx(0.5)
    assert decision["cold_start_improvement_fraction"] == pytest.approx(0.05)
    assert decision["gate"] == "cold_start_end_to_end"
    assert decision["promoted"] is False
    assert decision["recommendation"] == "keep-experimental"


def test_acceleration_decision_requires_semantic_parity():
    decision = acceleration_decision(
        legacy_local_seconds=1.0,
        candidate_local_seconds=0.5,
        parity=False,
    )

    assert decision["promoted"] is False
    assert decision["reason"] == "semantic parity failed"


def test_acceleration_decision_names_promoted_candidate():
    decision = acceleration_decision(
        legacy_local_seconds=1.0,
        candidate_local_seconds=0.5,
        candidate_name="native-fact-batches",
    )

    assert decision["recommendation"] == "promote-native-fact-batches"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"legacy_local_seconds": 0.0, "candidate_local_seconds": 1.0}, "positive"),
        ({"legacy_local_seconds": 1.0, "candidate_local_seconds": 0.0}, "positive"),
        (
            {
                "legacy_local_seconds": 1.0,
                "candidate_local_seconds": 0.5,
                "threshold": 1.0,
            },
            "threshold",
        ),
        (
            {
                "legacy_local_seconds": 1.0,
                "candidate_local_seconds": 0.5,
                "external_index_seconds": -1.0,
            },
            "non-negative",
        ),
        (
            {
                "legacy_local_seconds": float("nan"),
                "candidate_local_seconds": 0.5,
            },
            "finite and positive",
        ),
        (
            {
                "legacy_local_seconds": 1.0,
                "candidate_local_seconds": 0.5,
                "external_index_seconds": float("inf"),
            },
            "finite and non-negative",
        ),
    ],
)
def test_acceleration_decision_rejects_invalid_inputs(kwargs, message):
    with pytest.raises(ValueError, match=message):
        acceleration_decision(**kwargs)
