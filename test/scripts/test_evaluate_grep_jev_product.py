# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest
import requests

import scripts.evaluate_grep_jev_product as evaluation
from scripts.evaluate_grep_jev_product import evaluate, stop_reason, summarize


def test_failed_real_candidate_route_keeps_safe_diagnostics(monkeypatch, tmp_path):
    def snapshot(_repo, _commit, _tree, root):
        (root / "app.py").write_text("def run():\n    return 1\n")
        return {"files": 1}

    def planner(_payload, config, _key, budget):
        budget.before_model()
        budget.record("planning", config.planner_model, {"cost": 0.01})
        return evaluation.runtime.GrepPlan(
            actions=[
                {
                    "pattern": "run",
                    "glob": "**/*.py",
                    "case_sensitive": True,
                }
            ]
        )

    def failed_score(*_args):
        response = requests.Response()
        response.status_code = 503
        response._content = b"provider private details"
        raise requests.HTTPError("sensitive provider detail", response=response)

    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-fixture-key")
    monkeypatch.setattr(evaluation, "materialize_commit", snapshot)
    monkeypatch.setattr(evaluation.runtime, "_plan", planner)
    monkeypatch.setattr(evaluation.runtime, "decide_code_relevance", failed_score)
    monkeypatch.setattr(
        requests.Session,
        "send",
        lambda *_args, **_kwargs: pytest.fail("HTTP forbidden"),
    )
    result = evaluation.evaluate_case(
        {
            "repo": "owner/example",
            "instance_id": "case",
            "base_commit": "a" * 40,
            "git_tree": "b" * 40,
            "query": "Find run",
        },
        tmp_path,
        0.1,
    )

    assert result["status"] == "error"
    assert result["provider_failure"] == {"type": "HTTPError", "http_status": 503}
    assert result["search_plan"]["actions"][0]["pattern"] == "run"
    assert result["observed_candidates"][0]["file"] == "app.py"
    assert result["plan"]["unreported_call_cost"]
    assert "sensitive provider detail" not in str(result)
    assert "provider private details" not in str(result)
    assert "offline-fixture-key" not in str(result)


def test_no_billed_opt_in_stops_before_reading_inputs_or_credentials():
    with pytest.raises(ValueError, match="allow-billed-calls"):
        evaluate(SimpleNamespace(allow_billed_calls=False))


def test_unknown_failed_call_cost_stops_later_cases_before_spending_more():
    rows = [{"status": "complete", "plan": {"reported_cost_usd": 0.03}}]
    assert stop_reason(rows, 0.05) is None
    assert stop_reason(rows, 0.02) == "reported_cost_limit"
    rows.append({"status": "error", "plan": {"unreported_call_cost": True}})
    assert stop_reason(rows, 1) == "unreported_call_cost"
    rows[-1] = {"status": "error"}
    assert stop_reason(rows, 1) == "unreported_call_cost"


def test_failed_cases_stay_in_the_denominator_and_keep_reported_cost():
    result = summarize(
        [
            {
                "status": "complete",
                "grep_metrics": {"span_recall@5": 0.5},
                "reranked_metrics": {"span_recall@5": 1.0},
            },
            {
                "status": "error",
                "plan": {
                    "unreported_call_cost": True,
                    "provider_calls": [{"model": "planner", "usage": {"cost": 0.01}}],
                },
            },
        ]
    )
    assert result["cases"] == 2
    assert result["completed_cases"] == 1
    assert result["grep_recall_at_5"] == 0.25
    assert result["reranked_recall_at_5"] == 0.5
    assert result["reported_cost_usd"] == 0.01
    assert result["unreported_call_cost"]
    assert not result["model_evaluation_complete"]
