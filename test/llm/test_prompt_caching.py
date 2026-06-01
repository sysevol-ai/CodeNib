# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Anthropic prompt-cache breakpoint insertion (pure logic)."""

from __future__ import annotations

import pytest

from codeminer.llm.litellm_chat import _mark_cacheable, _with_prompt_caching

_CC = {"type": "ephemeral"}


def _last_block_cc(msg):
    """Return the cache_control on a message's last content block, or None."""
    content = msg.get("content")
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        return content[-1].get("cache_control")
    return None


def test_mark_cacheable_string_content_becomes_block():
    out = _mark_cacheable({"role": "system", "content": "hello"})
    assert out["content"] == [{"type": "text", "text": "hello", "cache_control": _CC}]


def test_mark_cacheable_noop_on_empty_or_none():
    # tool_calls-only assistant message has content=None — must not crash/alter.
    msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]}
    assert _mark_cacheable(msg) == msg
    assert _mark_cacheable({"role": "user", "content": ""}) == {
        "role": "user",
        "content": "",
    }


def test_claude_gets_system_and_last_breakpoints():
    msgs = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
        {"role": "tool", "content": "tool result"},
    ]
    out = _with_prompt_caching(msgs, "vertex_ai/claude-haiku-4-5")
    assert _last_block_cc(out[0]) == _CC  # system (stable: tools+system prefix)
    assert _last_block_cc(out[-1]) == _CC  # last message (rolling: conversation)
    # the middle messages are untouched
    assert out[1]["content"] == "q"
    assert out[2]["content"] is None


def test_non_anthropic_model_untouched():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    assert _with_prompt_caching(msgs, "gpt-4o") == msgs


def test_env_disable_is_respected(monkeypatch):
    monkeypatch.setenv("CODEMINER_DISABLE_PROMPT_CACHE", "1")
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    assert _with_prompt_caching(msgs, "claude-haiku-4-5") == msgs


def test_empty_messages_noop():
    assert _with_prompt_caching([], "claude-haiku-4-5") == []


@pytest.mark.parametrize(
    "model", ["claude-3-5-haiku", "anthropic/claude", "VERTEX_AI/CLAUDE-HAIKU"]
)
def test_anthropic_detection_variants(model):
    out = _with_prompt_caching([{"role": "user", "content": "q"}], model)
    assert _last_block_cc(out[-1]) == _CC
