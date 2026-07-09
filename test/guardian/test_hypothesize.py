# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codeminer.guardian.hypothesize."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.guardian.hypothesize import (
    Hypothesis,
    heuristic_hypotheses,
    hypothesize,
)
from codeminer.guardian.signals import Hotspot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hotspots(*paths_counts):
    return [Hotspot(path=p, commit_count=c) for p, c in paths_counts]


def _mock_llm(content: str):
    """Return a fake LiteLLMChat-like object whose _call_raw returns `content`."""
    msg = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=msg)
    resp = SimpleNamespace(choices=[choice], usage=SimpleNamespace(
        prompt_tokens=10, completion_tokens=20, total_tokens=30
    ))
    llm = MagicMock()
    llm._call_raw.return_value = resp
    return llm


def _valid_json_response(hypotheses):
    return json.dumps(hypotheses)


# ---------------------------------------------------------------------------
# heuristic_hypotheses
# ---------------------------------------------------------------------------


class TestHeuristicHypotheses:
    def test_ranks_by_commit_count_descending(self):
        spots = _hotspots(("b.py", 5), ("a.py", 20), ("c.py", 10))
        result = heuristic_hypotheses(spots)
        assert result[0].target == "a.py"
        assert result[1].target == "c.py"
        assert result[2].target == "b.py"

    def test_rank_field_is_sequential_from_one(self):
        spots = _hotspots(("x.py", 3), ("y.py", 1))
        result = heuristic_hypotheses(spots)
        assert [h.rank for h in result] == [1, 2]

    def test_highest_churn_has_max_confidence(self):
        spots = _hotspots(("hot.py", 100), ("cold.py", 1))
        result = heuristic_hypotheses(spots)
        assert result[0].confidence == pytest.approx(0.80)

    def test_lowest_churn_has_min_confidence(self):
        spots = _hotspots(("hot.py", 100), ("cold.py", 1))
        result = heuristic_hypotheses(spots)
        assert result[-1].confidence >= 0.30

    def test_top_n_limits_output(self):
        spots = _hotspots(*[(f"f{i}.py", 10 - i) for i in range(10)])
        result = heuristic_hypotheses(spots, top_n=3)
        assert len(result) == 3

    def test_empty_hotspots_returns_empty(self):
        assert heuristic_hypotheses([]) == []

    def test_drift_signals_appended_after_churn(self):
        from codeminer.guardian.graph_diff import DriftSignal

        spots = _hotspots(("a.py", 5))
        drift = [DriftSignal(kind="fan_in_spike", symbol="sym", file="b.py",
                             detail="gained 6 callers", edge_changes=[])]
        result = heuristic_hypotheses(spots, drift)
        assert result[0].kind == "churn"
        assert result[1].kind == "drift"
        assert result[1].target == "sym"

    def test_memory_cited_is_always_empty_for_heuristic(self):
        spots = _hotspots(("a.py", 5))
        result = heuristic_hypotheses(spots)
        assert result[0].memory_cited == []

    def test_statement_is_non_empty(self):
        spots = _hotspots(("mod.py", 7))
        result = heuristic_hypotheses(spots)
        assert len(result[0].statement) > 0


# ---------------------------------------------------------------------------
# hypothesize (LLM-backed)
# ---------------------------------------------------------------------------


class TestHypothesizeLLM:
    def _well_formed(self):
        return [
            {
                "rank": 1,
                "target": "a.py",
                "kind": "churn",
                "statement": "a.py accumulates risky mutations.",
                "confidence": 0.75,
                "memory_cited": ["prior finding about a.py"],
            },
            {
                "rank": 2,
                "target": "b.py",
                "kind": "churn",
                "statement": "b.py changed contract.",
                "confidence": 0.50,
                "memory_cited": [],
            },
        ]

    def test_returns_ranked_hypotheses(self):
        spots = _hotspots(("a.py", 10), ("b.py", 5))
        llm = _mock_llm(_valid_json_response(self._well_formed()))
        result = hypothesize(spots, [], [], llm)
        assert len(result) == 2
        assert result[0].rank == 1
        assert result[0].target == "a.py"
        assert result[0].memory_cited == ["prior finding about a.py"]

    def test_llm_called_once(self):
        spots = _hotspots(("a.py", 10))
        llm = _mock_llm(_valid_json_response(self._well_formed()[:1]))
        hypothesize(spots, [], [], llm)
        assert llm._call_raw.call_count == 1

    def test_usage_acc_is_updated(self):
        from codeminer.guardian.llm_investigator import LLMUsage

        spots = _hotspots(("a.py", 10))
        llm = _mock_llm(_valid_json_response(self._well_formed()[:1]))
        acc = LLMUsage()
        hypothesize(spots, [], [], llm, usage_acc=acc)
        assert acc.total_tokens == 30

    def test_malformed_json_falls_back_to_heuristic(self):
        spots = _hotspots(("a.py", 10), ("b.py", 5))
        llm = _mock_llm("not valid json {{")
        result = hypothesize(spots, [], [], llm)
        # Falls back: heuristic ranks a.py first (higher commit count)
        assert result[0].target == "a.py"
        assert result[0].memory_cited == []

    def test_llm_error_falls_back_to_heuristic(self):
        spots = _hotspots(("a.py", 10))
        llm = MagicMock()
        llm._call_raw.side_effect = RuntimeError("connection refused")
        result = hypothesize(spots, [], [], llm)
        assert result[0].target == "a.py"

    def test_model_returning_object_instead_of_array_falls_back(self):
        spots = _hotspots(("a.py", 10))
        llm = _mock_llm('{"rank": 1, "target": "a.py"}')  # object, not array
        result = hypothesize(spots, [], [], llm)
        assert result[0].target == "a.py"

    def test_empty_signals_returns_empty_without_calling_llm(self):
        llm = MagicMock()
        result = hypothesize([], [], [], llm)
        assert result == []
        llm._call_raw.assert_not_called()

    def test_top_n_limits_output(self):
        items = [
            {"rank": i, "target": f"f{i}.py", "kind": "churn",
             "statement": "s", "confidence": 0.5, "memory_cited": []}
            for i in range(1, 8)
        ]
        spots = _hotspots(*[(f"f{i}.py", 10) for i in range(1, 8)])
        llm = _mock_llm(_valid_json_response(items))
        result = hypothesize(spots, [], [], llm, top_n=3)
        assert len(result) <= 3

    def test_output_sorted_by_rank(self):
        # Model returns items out of order.
        items = [
            {"rank": 3, "target": "c.py", "kind": "churn",
             "statement": "s", "confidence": 0.3, "memory_cited": []},
            {"rank": 1, "target": "a.py", "kind": "churn",
             "statement": "s", "confidence": 0.9, "memory_cited": []},
            {"rank": 2, "target": "b.py", "kind": "churn",
             "statement": "s", "confidence": 0.6, "memory_cited": []},
        ]
        spots = _hotspots(("a.py", 10), ("b.py", 8), ("c.py", 6))
        llm = _mock_llm(_valid_json_response(items))
        result = hypothesize(spots, [], [], llm)
        assert [h.rank for h in result] == [1, 2, 3]

    def test_prior_findings_included_in_prompt(self):
        spots = _hotspots(("a.py", 5))
        prior = [{"commit_sha": "abc12345", "kind": "churn",
                   "title": "High-churn file: a.py", "detail": "changed 5 times"}]
        llm = _mock_llm(_valid_json_response(self._well_formed()[:1]))
        hypothesize(spots, [], prior, llm)
        call_args = llm._call_raw.call_args[0][0]
        user_content = next(m["content"] for m in call_args if m["role"] == "user")
        assert "a.py" in user_content
        assert "MEMORY" in user_content
