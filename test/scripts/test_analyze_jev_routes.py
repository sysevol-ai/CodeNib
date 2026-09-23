# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from collections import Counter

import pytest

from scripts.analyze_jev_routes import planner_attempts


def test_resumed_plans_retain_each_request_without_double_counting_summaries():
    legacy = {"http_status": 429, "usage": {"cost": 0.01, "prompt_tokens": 10}}
    prior_calls = [
        {"http_status": 429, "usage": {"cost": 0.02, "prompt_tokens": 20}},
        {"http_status": 429, "usage": {"cost": 0.03, "prompt_tokens": 30}},
    ]
    current_calls = [
        {"http_status": 429, "usage": {"cost": 0.04, "prompt_tokens": 40}},
        {"http_status": 200, "usage": {"cost": 0.05, "prompt_tokens": 50}},
    ]
    prior = {**prior_calls[-1], "attempts": prior_calls, "prior_run_attempt": legacy}
    current = {
        **current_calls[-1],
        "attempts": current_calls,
        "prior_run_attempt": prior,
    }

    attempts = planner_attempts(current)

    assert attempts == [legacy, *prior_calls, *current_calls]
    assert Counter(a["http_status"] for a in attempts) == {429: 4, 200: 1}
    assert sum(a["usage"]["cost"] for a in attempts) == pytest.approx(0.15)
    assert sum(a["usage"]["prompt_tokens"] for a in attempts) == 150


def test_legacy_single_attempt_is_retained():
    plan = {"http_status": 200, "usage": {"cost": 0.1}}

    assert planner_attempts(plan) == [plan]
