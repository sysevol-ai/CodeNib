# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent-runner promotion evidence gates."""

from __future__ import annotations

import pytest

from codeminer.eval.agent_runner.promotion import (
    EvidenceSlice,
    PromotionEvidence,
    evaluate_promotion_evidence,
)


def _base_evidence(**overrides):
    values = {
        "runtime_contract": "Context items include provenance and consumption state",
        "raw_behavior_delta": "Agents consume fewer stale context items in traces",
        "failure_category": "context_runtime",
        "slices": (
            EvidenceSlice(
                name="weekly-smoke",
                role="smoke",
                instance_count=3,
                seed="stable",
            ),
            EvidenceSlice(
                name="weekly-holdout",
                role="holdout",
                instance_count=6,
                seed="week-12",
            ),
        ),
    }
    values.update(overrides)
    return PromotionEvidence(**values)


def test_promotion_evidence_ready_with_smoke_and_holdout():
    result = evaluate_promotion_evidence(_base_evidence())

    assert result.ready is True
    assert result.failures == ()


def test_promotion_evidence_requires_holdout_slice():
    evidence = _base_evidence(
        slices=(
            EvidenceSlice(
                name="weekly-smoke",
                role="smoke",
                instance_count=3,
            ),
        )
    )

    result = evaluate_promotion_evidence(evidence)

    assert result.ready is False
    assert "at least one non-empty holdout slice is required" in result.failures


def test_promotion_evidence_rejects_scorer_and_instance_dependencies():
    evidence = _base_evidence(
        scorer_dependencies=("answer_order_field",),
        benchmark_dependencies=("project-x__case-1",),
    )

    result = evaluate_promotion_evidence(evidence)

    assert result.ready is False
    assert "promotion must not depend on scorer-specific fields" in result.failures
    assert "promotion must not depend on named benchmark instances" in result.failures


def test_model_specific_evidence_requires_comparison_surface():
    evidence = _base_evidence(
        model_specific=True,
        compared_models=("model-a",),
    )

    result = evaluate_promotion_evidence(evidence)

    assert result.ready is False
    assert (
        "model_specific changes require at least two compared systems"
        in result.failures
    )


def test_promotion_evidence_serializes_normalized_values():
    evidence = _base_evidence(
        compared_models=(" model-a ", ""),
        compared_agents=("agent-b",),
        notes="kept as evaluation policy",
    )

    assert evidence.to_dict() == {
        "runtime_contract": "Context items include provenance and consumption state",
        "raw_behavior_delta": "Agents consume fewer stale context items in traces",
        "failure_category": "context_runtime",
        "slices": [
            {
                "name": "weekly-smoke",
                "role": "smoke",
                "instance_count": 3,
                "report_path": None,
                "seed": "stable",
            },
            {
                "name": "weekly-holdout",
                "role": "holdout",
                "instance_count": 6,
                "report_path": None,
                "seed": "week-12",
            },
        ],
        "compared_models": ["model-a"],
        "compared_agents": ["agent-b"],
        "model_specific": False,
        "scorer_dependencies": [],
        "benchmark_dependencies": [],
        "notes": "kept as evaluation policy",
    }


def test_evidence_slice_rejects_invalid_role():
    with pytest.raises(ValueError, match="role"):
        EvidenceSlice(name="bad", role="training", instance_count=1)
