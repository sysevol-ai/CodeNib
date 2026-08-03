# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for agent-runner candidate orchestration helpers."""

from __future__ import annotations

from codenib.agent.agent_types import AgentResult, ToolCallRecord
from codenib.eval.agent_runner.orchestrator import (
    query_is_specific,
    scatter_gather_localize,
    verdict_is_yes,
)


def _result(answer: str, skill_id: str, turns: int = 1) -> AgentResult:
    return AgentResult(
        answer=answer,
        tool_calls=[ToolCallRecord(skill_id, skill_id, {})],
        total_turns=turns,
        total_duration_ms=float(turns),
    )


def test_query_is_specific_defaults_to_eager_on_ambiguity_or_failure():
    assert query_is_specific(lambda messages: "Maybe", "where is auth?") is True
    assert query_is_specific(
        lambda messages: (_ for _ in ()).throw(RuntimeError()), "q"
    )


def test_query_is_specific_uses_last_verdict_token():
    assert (
        query_is_specific(lambda messages: "I first thought EAGER. GREP", "q") is False
    )
    assert query_is_specific(lambda messages: "GREP then EAGER", "q") is True


def test_verdict_is_yes_uses_last_verdict_line():
    assert verdict_is_yes("VERDICT: no\nlater\nVERDICT: yes")
    assert not verdict_is_yes("VERDICT: yes\nlater\nVERDICT: no")


def test_scatter_gather_explores_when_no_candidates():
    calls = []

    def run_subagent(system_prompt, query, max_turns):
        calls.append((system_prompt, query, max_turns))
        return _result("Locations: pkg/f.py:1-2", "explore", turns=max_turns)

    result = scatter_gather_localize("task", [], run_subagent, explore_turns=7)

    assert result.answer == "Locations: pkg/f.py:1-2"
    assert calls == [(None, "task", 7)]


def test_scatter_gather_converges_confirmed_candidates():
    candidates = [{"file": "pkg/f.py", "start": 10, "end": 12}]
    calls = []

    def run_subagent(system_prompt, query, max_turns):
        calls.append((system_prompt, query, max_turns))
        if system_prompt is not None:
            return _result("VERDICT: yes", "verify", turns=2)
        return _result("Locations: pkg/f.py:10-12", "final", turns=1)

    result = scatter_gather_localize(
        "task",
        candidates,
        run_subagent,
        sub_turns=2,
        explore_turns=9,
    )

    assert result.answer == "Locations: pkg/f.py:10-12"
    assert result.total_turns == 3
    assert [call.skill_id for call in result.tool_calls] == ["verify", "final"]
    assert calls[0][0] is not None
    assert "pkg/f.py:10-12" in calls[0][1]
    assert calls[1][0] is None
    assert "CONFIRMED relevant" in calls[1][1]
    assert calls[1][2] == 2


def test_scatter_gather_falls_back_when_candidates_rejected():
    candidates = [{"file": "pkg/f.py", "start": 10, "end": 12}]
    calls = []

    def run_subagent(system_prompt, query, max_turns):
        calls.append((system_prompt, query, max_turns))
        if system_prompt is not None:
            return _result("VERDICT: no", "verify", turns=2)
        return _result("Locations: pkg/other.py:1-2", "explore", turns=max_turns)

    result = scatter_gather_localize(
        "task",
        candidates,
        run_subagent,
        sub_turns=2,
        explore_turns=9,
    )

    assert result.answer == "Locations: pkg/other.py:1-2"
    assert result.total_turns == 11
    assert calls[-1] == (None, "task", 9)
