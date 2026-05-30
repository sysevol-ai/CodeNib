# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for token-usage tracking in AgentRunner / LiteLLMChat / UsageTracker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.llm.usage import (
    TokenUsage,
    UsageRecord,
    UsageTracker,
    _extract_token_usage,
)

# ---------------------------------------------------------------------------
# Helpers (mirrors test_runner.py patterns)
# ---------------------------------------------------------------------------


def _make_response(
    content=None,
    tool_calls=None,
    prompt_tokens=0,
    completion_tokens=0,
    total_tokens=None,
):
    """Build a fake litellm response with a ``usage`` field."""
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    msg = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def _make_tool_call(tc_id, name, arguments_json):
    return SimpleNamespace(
        id=tc_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments_json),
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


@pytest.fixture()
def echo_registry():
    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            skill_id="echo",
            skill_type=SkillType.CUSTOM,
            inputs=[SkillInputSpec(name="text", type_hint="str", required=True)],
            outputs=SkillOutputSpec(type_hint="str"),
            executor_fn=lambda text: f"echo: {text}",
        )
    )
    return reg


# ---------------------------------------------------------------------------
# TokenUsage / UsageTracker unit tests
# ---------------------------------------------------------------------------


class TestTokenUsage:
    def test_add_both_with_cost(self):
        a = TokenUsage(10, 20, 30, cost_usd=0.01)
        b = TokenUsage(5, 5, 10, cost_usd=0.02)
        c = a.add(b)
        assert c.prompt_tokens == 15
        assert c.completion_tokens == 25
        assert c.total_tokens == 40
        assert c.cost_usd == pytest.approx(0.03)

    def test_add_missing_cost_left(self):
        a = TokenUsage(10, 20, 30)
        b = TokenUsage(5, 5, 10, cost_usd=0.02)
        assert a.add(b).cost_usd == pytest.approx(0.02)

    def test_add_both_missing_cost(self):
        a = TokenUsage(10, 20, 30)
        b = TokenUsage(5, 5, 10)
        assert a.add(b).cost_usd is None

    def test_to_dict(self):
        d = TokenUsage(1, 2, 3, cost_usd=0.5).to_dict()
        assert d == {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "cost_usd": 0.5,
        }


class TestExtractTokenUsage:
    def test_object_usage(self):
        resp = _make_response(prompt_tokens=10, completion_tokens=20)
        u = _extract_token_usage(resp)
        assert u.prompt_tokens == 10
        assert u.completion_tokens == 20
        assert u.total_tokens == 30

    def test_dict_usage(self):
        resp = SimpleNamespace(
            usage={
                "prompt_tokens": 4,
                "completion_tokens": 6,
                "total_tokens": 10,
            }
        )
        u = _extract_token_usage(resp)
        assert u.prompt_tokens == 4
        assert u.completion_tokens == 6
        assert u.total_tokens == 10

    def test_missing_usage(self):
        resp = SimpleNamespace()
        u = _extract_token_usage(resp)
        assert u == TokenUsage()


class TestUsageTracker:
    def test_record_and_totals(self):
        tracker = UsageTracker()
        with patch("codeminer.llm.usage._safe_completion_cost", return_value=None):
            tracker.record_response(
                _make_response(prompt_tokens=10, completion_tokens=20),
                model="gpt-4o",
                duration_ms=123.4,
                turn=1,
            )
            tracker.record_response(
                _make_response(prompt_tokens=5, completion_tokens=5),
                model="gpt-4o",
                turn=2,
            )

        assert len(tracker.records) == 2
        assert isinstance(tracker.records[0], UsageRecord)
        assert tracker.records[0].turn == 1
        assert tracker.records[0].duration_ms == pytest.approx(123.4)

        total = tracker.totals()
        assert total.prompt_tokens == 15
        assert total.completion_tokens == 25
        assert total.total_tokens == 40
        assert total.cost_usd is None

    def test_cost_aggregation_when_available(self):
        tracker = UsageTracker()
        with patch(
            "codeminer.llm.usage._safe_completion_cost",
            side_effect=[0.001, 0.002],
        ):
            tracker.record_response(
                _make_response(prompt_tokens=10, completion_tokens=20),
                model="gpt-4o",
            )
            tracker.record_response(
                _make_response(prompt_tokens=10, completion_tokens=20),
                model="gpt-4o",
            )
        assert tracker.totals().cost_usd == pytest.approx(0.003)

    def test_completion_cost_failure_is_none(self):
        """If litellm.completion_cost raises, cost_usd is None — not a crash."""
        tracker = UsageTracker()
        with patch("litellm.completion_cost", side_effect=RuntimeError("unpriced")):
            tracker.record_response(
                _make_response(prompt_tokens=3, completion_tokens=4),
                model="fake-model",
            )
        assert tracker.records[0].usage.cost_usd is None
        assert tracker.totals().total_tokens == 7


# ---------------------------------------------------------------------------
# LiteLLMChat._call_raw usage_tracker wiring
# ---------------------------------------------------------------------------


class TestLiteLLMChatUsageTracker:
    def test_usage_tracker_receives_record(self):
        chat = LiteLLMChat(model="gpt-4o")
        tracker = UsageTracker()
        fake_response = _make_response(
            content="ok", prompt_tokens=7, completion_tokens=8
        )

        with (
            patch(
                "codeminer.llm.litellm_chat.litellm.completion",
                return_value=fake_response,
            ),
            patch("codeminer.llm.usage._safe_completion_cost", return_value=None),
        ):
            resp = chat._call_raw(
                [{"role": "user", "content": "hi"}],
                usage_tracker=tracker,
                usage_turn=1,
            )

        assert resp is fake_response
        assert len(tracker.records) == 1
        rec = tracker.records[0]
        assert rec.model == "gpt-4o"
        assert rec.turn == 1
        assert rec.usage.prompt_tokens == 7
        assert rec.usage.completion_tokens == 8
        assert rec.usage.total_tokens == 15
        assert rec.duration_ms >= 0.0

    def test_usage_tracker_not_forwarded_to_litellm(self):
        """usage_tracker/usage_turn must not leak into litellm.completion kwargs."""
        chat = LiteLLMChat(model="gpt-4o")
        tracker = UsageTracker()
        with (
            patch("codeminer.llm.litellm_chat.litellm.completion") as mock_completion,
            patch("codeminer.llm.usage._safe_completion_cost", return_value=None),
        ):
            mock_completion.return_value = _make_response(content="ok")
            chat._call_raw(
                [{"role": "user", "content": "hi"}],
                usage_tracker=tracker,
                usage_turn=3,
            )
            kwargs = mock_completion.call_args.kwargs
            assert "usage_tracker" not in kwargs
            assert "usage_turn" not in kwargs


# ---------------------------------------------------------------------------
# AgentRunner populates AgentResult.usage
# ---------------------------------------------------------------------------


class TestAgentRunnerUsage:
    def test_usage_populated_direct_answer(self):
        """Single LLM call with no tools → usage.total_tokens matches response."""
        llm = MagicMock(spec=LiteLLMChat)

        def _call_raw(messages, **kwargs):
            tracker = kwargs.get("usage_tracker")
            resp = _make_response(
                content="Final answer", prompt_tokens=100, completion_tokens=50
            )
            if tracker is not None:
                with patch(
                    "codeminer.llm.usage._safe_completion_cost",
                    return_value=None,
                ):
                    tracker.record_response(
                        resp, model="test-model", turn=kwargs.get("usage_turn")
                    )
            return resp

        llm._call_raw.side_effect = _call_raw

        runner = AgentRunner(llm, SkillRegistry())
        result = runner.run("hello")

        assert result.answer == "Final answer"
        assert result.usage is not None
        assert result.usage.prompt_tokens == 100
        assert result.usage.completion_tokens == 50
        assert result.usage.total_tokens == 150
        assert len(result.usage_records) == 1
        assert result.usage_records[0].turn == 1

    def test_usage_aggregates_across_turns(self, echo_registry):
        """Two-turn run aggregates prompt/completion tokens from both responses."""
        llm = MagicMock(spec=LiteLLMChat)
        tc = _make_tool_call("call_1", "echo", '{"text": "hi"}')
        responses = [
            _make_response(tool_calls=[tc], prompt_tokens=40, completion_tokens=10),
            _make_response(content="Done", prompt_tokens=60, completion_tokens=20),
        ]
        call_count = {"i": 0}

        def _call_raw(messages, **kwargs):
            tracker = kwargs.get("usage_tracker")
            resp = responses[call_count["i"]]
            call_count["i"] += 1
            if tracker is not None:
                with patch(
                    "codeminer.llm.usage._safe_completion_cost",
                    return_value=None,
                ):
                    tracker.record_response(
                        resp, model="test-model", turn=kwargs.get("usage_turn")
                    )
            return resp

        llm._call_raw.side_effect = _call_raw

        runner = AgentRunner(llm, echo_registry)
        result = runner.run("echo hi")

        assert result.total_turns == 2
        assert result.usage is not None
        assert result.usage.prompt_tokens == 100
        assert result.usage.completion_tokens == 30
        assert result.usage.total_tokens == 130
        assert len(result.usage_records) == 2
        assert [r.turn for r in result.usage_records] == [1, 2]

    def test_call_raw_receives_usage_tracker_kwarg(self):
        """Runner always forwards usage_tracker + usage_turn to _call_raw."""
        llm = MagicMock(spec=LiteLLMChat)
        llm._call_raw.return_value = _make_response(content="ok")

        runner = AgentRunner(llm, SkillRegistry())
        runner.run("test")

        assert llm._call_raw.called
        kwargs = llm._call_raw.call_args.kwargs
        assert "usage_tracker" in kwargs
        assert kwargs["usage_turn"] == 1

    def test_usage_populated_on_max_turns_exhausted(self, echo_registry):
        """If the LLM never stops calling tools, the max_turns-exhausted
        return path must still populate ``usage`` / ``usage_records``.
        """
        llm = MagicMock(spec=LiteLLMChat)

        def _call_raw(messages, **kwargs):
            # Always return a tool_call → never reach the terminal branch.
            tracker = kwargs.get("usage_tracker")
            tc = _make_tool_call("call_x", "echo", '{"text": "loop"}')
            resp = _make_response(tool_calls=[tc], prompt_tokens=7, completion_tokens=3)
            if tracker is not None:
                with patch(
                    "codeminer.llm.usage._safe_completion_cost",
                    return_value=None,
                ):
                    tracker.record_response(
                        resp, model="test-model", turn=kwargs.get("usage_turn")
                    )
            return resp

        llm._call_raw.side_effect = _call_raw

        runner = AgentRunner(llm, echo_registry, max_turns=2)
        result = runner.run("loop forever")

        # Budget exhausted with no prose → the runner makes one forced
        # tool-free summary turn (usage_turn = max_turns + 1), whose token
        # usage must also be tracked. total_turns stays at the loop count.
        assert result.total_turns == 2
        assert llm._call_raw.call_count == 3  # 2 loop turns + 1 forced summary
        # Crucial assertion: usage on the max-turns return path.
        assert result.usage is not None
        assert result.usage.prompt_tokens == 21  # 3 × 7
        assert result.usage.completion_tokens == 9  # 3 × 3
        assert result.usage.total_tokens == 30
        assert len(result.usage_records) == 3
        assert [r.turn for r in result.usage_records] == [1, 2, 3]
