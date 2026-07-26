# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for token-aware, summary-based Guardian context compaction."""

from codeminer.guardian.loop import CycleState
from codeminer.guardian.loop.context import (
    compact_messages,
    initial_messages,
    needs_compaction,
    summarization_messages,
)


def _state():
    return CycleState(
        cycle_no=1,
        commit="abc123",
        hypotheses=[],
        signals=[],
        current=None,
        budget_total=10_000,
        budget_spent=0,
        decision_log=[],
        exit_reason=None,
        carried_from=None,
    )


def _turn(call_id: str, observation: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id}],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": observation,
        },
    ]


def test_tool_results_stay_in_context_before_token_boundary():
    messages = initial_messages(_state(), repo_path="/repo", arm="memory")
    messages.extend(_turn("c1", "important source " * 100))

    assert not needs_compaction(
        messages,
        max_tokens=10_000,
        reserve_tokens=1_000,
    )
    assert messages[-1]["content"].startswith("important source")


def test_compaction_preserves_frame_summary_and_recent_turns(tmp_path):
    state = _state()
    messages = initial_messages(state, repo_path="/repo", arm="memory")
    frame = [dict(messages[0]), dict(messages[1])]
    messages.extend(_turn("c1", "RAW_OLD_TOOL_PAYLOAD"))
    messages.extend(_turn("c2", "recent observation"))

    compacted, events = compact_messages(
        messages,
        summary="The old observation ruled out the parser hypothesis.",
        state=state,
        output_dir=tmp_path / "context",
        keep_recent_turns=1,
    )

    assert compacted[:2] == frame
    assert "ruled out the parser hypothesis" in compacted[2]["content"]
    assert compacted[-1]["content"] == "recent observation"
    assert "RAW_OLD_TOOL_PAYLOAD" not in str(compacted)
    assert events == [
        {
            "event": "compaction",
            "strategy": "structured_summary",
            "archive": "transcript_before_compaction_0001.json",
            "messages_before": 6,
            "messages_after": 5,
            "recent_turns_retained": 1,
            "summary_chars": 52,
        }
    ]
    archive = tmp_path / "context" / events[0]["archive"]
    assert "RAW_OLD_TOOL_PAYLOAD" in archive.read_text(encoding="utf-8")


def test_summarization_request_appends_to_cacheable_history():
    state = _state()
    messages = initial_messages(state, repo_path="/repo", arm="memory")
    messages.extend(_turn("c1", "source result"))

    request = summarization_messages(messages, state)

    assert request[:-1] == messages
    assert "Return only the working-memory summary" in request[-1]["content"]
    assert '"commit": "abc123"' in request[-1]["content"]


def test_compaction_policy_is_arm_blind(tmp_path):
    state = _state()
    memory = initial_messages(state, repo_path="/repo", arm="memory")
    memoryless = initial_messages(state, repo_path="/repo", arm="memoryless")
    memory.extend(_turn("c1", "same observation"))
    memoryless.extend(_turn("c1", "same observation"))

    compacted_memory, events_memory = compact_messages(
        memory,
        summary="same summary",
        state=state,
        output_dir=tmp_path / "memory",
    )
    compacted_memoryless, events_memoryless = compact_messages(
        memoryless,
        summary="same summary",
        state=state,
        output_dir=tmp_path / "memoryless",
    )

    assert events_memory[0]["strategy"] == events_memoryless[0]["strategy"]
    assert compacted_memory[2:] == compacted_memoryless[2:]


def test_unlimited_budget_is_explicit_in_immutable_frame():
    state = _state()
    state.budget_total = None

    messages = initial_messages(state, repo_path="/repo", arm="memory")

    assert "Token budget: unlimited" in messages[1]["content"]
