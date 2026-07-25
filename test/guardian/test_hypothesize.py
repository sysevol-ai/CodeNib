# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Compatibility tests for isolated hypothesis formation.

Production cycles use the multi-turn outer loop; these cover only the degraded
fallback and the one-shot evaluation adapter.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from codeminer.guardian.orchestrator import heuristic_hypotheses, hypothesize
from codeminer.guardian.signals import Hotspot


def _hotspots(*paths_counts):
    return [Hotspot(path=path, commit_count=count) for path, count in paths_counts]


def _mock_llm(content):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
    )
    llm = MagicMock()
    llm._call_raw.return_value = response
    return llm


def test_degraded_fallback_ranks_churn_and_is_complete():
    result = heuristic_hypotheses(
        _hotspots(("cold.py", 1), ("hot.py", 20), ("warm.py", 5))
    )
    assert [item.target for item in result] == ["hot.py", "warm.py", "cold.py"]
    assert all(item.claim and item.consequence and item.remedy for item in result)
    assert all(item.origin == "signal" for item in result)
    assert all(item.grade == "conjecture" for item in result)


def test_degraded_fallback_includes_drift_as_signal_origin():
    from codeminer.guardian.signals.graph_diff import DriftSignal

    result = heuristic_hypotheses(
        _hotspots(("a.py", 5)),
        [
            DriftSignal(
                kind="fan_in_spike",
                symbol="sym",
                file="b.py",
                detail="gained callers",
            )
        ],
    )
    assert result[1].locus == ["b.py", "sym"]
    assert result[1].origin == "signal"


def test_one_shot_adapter_parses_canonical_hypothesis():
    payload = [
        {
            "claim": "a.parse accepts invalid input",
            "consequence": "callers receive invalid state",
            "remedy": "validate input and add a regression test",
            "origin": "exploration",
            "locus": ["a.py:parse"],
            "evidence": [],
            "confidence": 0.75,
        }
    ]
    llm = _mock_llm(json.dumps(payload))
    result = hypothesize(_hotspots(("a.py", 10)), [], [], llm)
    assert len(result) == 1
    assert result[0].claim.startswith("a.parse")
    assert result[0].origin == "exploration"
    assert llm._call_raw.call_count == 1


def test_one_shot_adapter_falls_back_and_marks_signal_origin():
    result = hypothesize(
        _hotspots(("a.py", 10)),
        [],
        [],
        _mock_llm("not-json"),
    )
    assert result[0].target == "a.py"
    assert result[0].origin == "signal"


def test_one_shot_adapter_can_form_memory_or_exploration_origin():
    payload = [
        {
            "claim": "two config helpers disagree on empty input",
            "consequence": "callers see inconsistent behavior",
            "remedy": "share validation and add a regression test",
            "origin": "memory",
            "locus": ["a.py:parse", "b.py:normalize"],
            "evidence": ["cycle:1"],
            "confidence": 0.7,
        }
    ]
    result = hypothesize(
        [],
        [],
        [{"claim": "prior config mismatch"}],
        _mock_llm(json.dumps(payload)),
    )
    assert result[0].origin == "memory"
    assert not any(ref.startswith("signal:") for ref in result[0].evidence)
