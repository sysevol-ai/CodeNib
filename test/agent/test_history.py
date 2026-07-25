# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for TokenBudgetedChatHistory (issue #109, phase 4).

A token-budgeted history caps total context tokens and evicts the OLDEST
non-system messages first when the budget would be exceeded, always
preserving the system prompt and the most recent turns.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codenib.agent.history import (
    PlainChatHistory,
    TokenBudgetedChatHistory,
    count_message_tokens,
)


def _msg(role, content):
    return {"role": role, "content": content}


# ---------------------------------------------------------------------------
# count_message_tokens
# ---------------------------------------------------------------------------


class TestCountMessageTokens:
    def test_empty_is_zero(self):
        assert count_message_tokens([]) == 0

    def test_char_heuristic_grows_with_content(self):
        small = count_message_tokens([_msg("user", "hi")])
        big = count_message_tokens([_msg("user", "x" * 400)])
        assert big > small

    def test_counts_tool_call_arguments(self):
        plain = count_message_tokens([{"role": "assistant", "content": "hi"}])
        with_tc = count_message_tokens(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "bm25_search",
                                "arguments": '{"query": "' + "x" * 200 + '"}',
                            },
                        }
                    ],
                }
            ]
        )
        assert with_tc > plain

    def test_uses_litellm_token_counter_when_model_given(self):
        # When a model is supplied and litellm.token_counter succeeds, its
        # result is used verbatim (not the char heuristic).
        import sys

        fake_litellm = MagicMock()
        fake_litellm.token_counter.return_value = 1234
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "litellm", fake_litellm)
            n = count_message_tokens([_msg("user", "hi")], model="gpt-4o")
        assert n == 1234
        fake_litellm.token_counter.assert_called_once()

    def test_falls_back_when_token_counter_raises(self):
        import sys

        fake_litellm = MagicMock()
        fake_litellm.token_counter.side_effect = RuntimeError("no tokenizer")
        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(sys.modules, "litellm", fake_litellm)
            n = count_message_tokens([_msg("user", "x" * 400)], model="weird-model")
        # Fell back to the char heuristic → positive count, no crash.
        assert n > 0


# ---------------------------------------------------------------------------
# TokenBudgetedChatHistory
# ---------------------------------------------------------------------------


class TestTokenBudgetedChatHistory:
    def test_rejects_nonpositive_budget(self):
        with pytest.raises(ValueError):
            TokenBudgetedChatHistory(0)
        with pytest.raises(ValueError):
            TokenBudgetedChatHistory(-5)

    def test_under_budget_keeps_everything(self):
        h = TokenBudgetedChatHistory(max_tokens=10_000, keep_last=2)
        h.add_message(_msg("system", "system prompt"))
        h.add_message(_msg("user", "question"))
        h.add_message(_msg("assistant", "answer"))
        assert len(h) == 3

    def test_evicts_oldest_nonsystem_when_over_budget(self):
        """Adding beyond the budget evicts oldest non-system; keeps system + recent."""
        # Each message ~= 40 chars → ~14 tokens via the heuristic. Budget of 40
        # tokens fits the system message plus only the most recent couple.
        h = TokenBudgetedChatHistory(max_tokens=40, keep_last=1)
        h.add_message(_msg("system", "SYSTEM-PROMPT-PINNED-" + "s" * 20))
        for i in range(6):
            h.add_message(_msg("user", f"turn-{i}-" + "x" * 32))

        roles = [m["role"] for m in h.get_messages()]
        # System prompt is always first and preserved.
        assert roles[0] == "system"
        assert sum(1 for r in roles if r == "system") == 1
        # The most recent message (the live tail) is preserved.
        assert h.get_messages()[-1]["content"].startswith("turn-5-")
        # Older non-system messages were evicted: fewer messages than added.
        assert len(h) < 7
        # turn-0 (the oldest non-system message) must be gone.
        contents = [m["content"] for m in h.get_messages()]
        assert not any(c.startswith("turn-0-") for c in contents)

    def test_system_message_never_evicted(self):
        h = TokenBudgetedChatHistory(max_tokens=20, keep_last=1)
        h.add_message(_msg("system", "S" * 200))  # alone over budget
        for _ in range(5):
            h.add_message(_msg("user", "u" * 80))
        roles = [m["role"] for m in h.get_messages()]
        assert "system" in roles
        assert roles[0] == "system"

    def test_keep_last_protects_recent_tail(self):
        """The last keep_last non-system messages survive even under pressure."""
        h = TokenBudgetedChatHistory(max_tokens=60, keep_last=3)
        h.add_message(_msg("system", "sys"))
        for i in range(10):
            h.add_message(_msg("user", f"m{i}-" + "z" * 40))
        contents = [m["content"] for m in h.get_messages() if m["role"] != "system"]
        # The three most-recent messages are always retained.
        assert contents[-3:] == [
            "m7-" + "z" * 40,
            "m8-" + "z" * 40,
            "m9-" + "z" * 40,
        ]

    def test_single_oversized_message_is_kept(self):
        """A lone message over budget is not dropped (we never drop what we just
        added when nothing else is evictable)."""
        h = TokenBudgetedChatHistory(max_tokens=10, keep_last=1)
        h.add_message(_msg("system", "sys"))
        h.add_message(_msg("user", "u" * 1000))
        # Both remain: system is pinned, the user message is the protected tail.
        assert len(h) == 2
        assert h.total_tokens() > h.max_tokens  # best-effort cap, not a guarantee

    def test_keep_last_zero_can_evict_all_nonsystem(self):
        h = TokenBudgetedChatHistory(max_tokens=15, keep_last=0)
        h.add_message(_msg("system", "sys"))
        for _ in range(4):
            h.add_message(_msg("user", "u" * 80))
        # With keep_last=0 the tail isn't protected, so eviction can remove
        # everything non-system if the last message itself fits the budget.
        roles = [m["role"] for m in h.get_messages()]
        assert roles[0] == "system"

    def test_clear(self):
        h = TokenBudgetedChatHistory(max_tokens=1000)
        h.add_message(_msg("system", "s"))
        h.add_message(_msg("user", "u"))
        h.clear()
        assert len(h) == 0
        assert h.get_messages() == []

    def test_iter_and_get_messages_are_live(self):
        h = TokenBudgetedChatHistory(max_tokens=1000)
        h.add_message(_msg("user", "a"))
        assert list(h) == h.get_messages()


# ---------------------------------------------------------------------------
# PlainChatHistory (unbounded shim)
# ---------------------------------------------------------------------------


class TestPlainChatHistory:
    def test_never_evicts(self):
        h = PlainChatHistory()
        for _ in range(50):
            h.add_message(_msg("user", "x" * 1000))
        assert len(h) == 50

    def test_extend_and_clear(self):
        h = PlainChatHistory()
        h.extend([_msg("user", "a"), _msg("user", "b")])
        assert len(h) == 2
        h.clear()
        assert len(h) == 0
