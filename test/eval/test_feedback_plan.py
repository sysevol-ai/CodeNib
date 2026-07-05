# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for small agent feedback plan selection."""

from __future__ import annotations

import pytest

from codeminer.eval.agent_runner.feedback import FeedbackPlanSpec, build_feedback_plan


def _rows():
    return {
        "py1": {"instance_id": "py1", "language_group": "Python"},
        "py2": {"instance_id": "py2", "language_group": "Python"},
        "py3": {"instance_id": "py3", "language_group": "Python"},
        "go1": {"instance_id": "go1", "language_group": "Go"},
        "go2": {"instance_id": "go2", "language_group": "Go"},
        "go3": {"instance_id": "go3", "language_group": "Go"},
    }


def test_feedback_plan_selects_smoke_and_holdout_per_group():
    plan = build_feedback_plan(
        _rows(),
        FeedbackPlanSpec(seed="week-1", smoke_per_group=1, holdout_per_group=2),
    )

    assert sorted(plan.groups) == ["Go", "Python"]
    assert len(plan.smoke_instances) == 2
    assert len(plan.holdout_instances) == 4
    assert set(plan.smoke_instances).isdisjoint(plan.holdout_instances)
    assert plan.instances == plan.smoke_instances + plan.holdout_instances


def test_feedback_plan_is_deterministic_for_same_seed():
    spec = FeedbackPlanSpec(seed="week-1", smoke_per_group=1, holdout_per_group=1)

    first = build_feedback_plan(_rows(), spec).to_dict()
    second = build_feedback_plan(dict(reversed(list(_rows().items()))), spec).to_dict()

    assert first == second


def test_feedback_plan_rotates_holdout_when_seed_changes():
    spec_a = FeedbackPlanSpec(seed="week-1", smoke_per_group=0, holdout_per_group=1)
    spec_b = FeedbackPlanSpec(seed="week-2", smoke_per_group=0, holdout_per_group=1)

    assert (
        build_feedback_plan(_rows(), spec_a).holdout_instances
        != build_feedback_plan(_rows(), spec_b).holdout_instances
    )


def test_feedback_plan_respects_excluded_instances():
    plan = build_feedback_plan(
        _rows(),
        FeedbackPlanSpec(
            seed="week-1",
            smoke_per_group=3,
            holdout_per_group=3,
            exclude_instances=("py1", "go2"),
        ),
    )

    assert "py1" not in plan.instances
    assert "go2" not in plan.instances


def test_feedback_plan_rejects_negative_counts():
    with pytest.raises(ValueError, match="smoke_per_group"):
        FeedbackPlanSpec(smoke_per_group=-1)
