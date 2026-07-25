# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for arm-blind externalize-and-reference context compaction."""

from codeminer.guardian.loop import CycleState
from codeminer.guardian.loop.context import compact_messages, initial_messages


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


def test_compaction_never_changes_frame_and_externalizes_observation(tmp_path):
    messages = initial_messages(_state(), repo_path="/repo", arm="memory")
    frame = [dict(messages[0]), dict(messages[1])]
    messages.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1"}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "large observation " * 2_000,
            },
        ]
    )
    compacted, events = compact_messages(
        messages,
        max_chars=2_000,
        output_dir=tmp_path / "observations",
    )
    assert compacted[:2] == frame
    assert len(events) == 1
    assert "externalized" in compacted[-1]["content"]
    ref = events[0]["observation_ref"]
    assert (tmp_path / "observations" / ref).read_text().startswith("large observation")


def test_compaction_policy_is_arm_blind(tmp_path):
    memory = initial_messages(_state(), repo_path="/repo", arm="memory")
    memoryless = initial_messages(_state(), repo_path="/repo", arm="memoryless")
    observation = {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "same observation " * 2_000,
    }
    memory.append(observation)
    memoryless.append(observation)
    compacted_memory, events_memory = compact_messages(
        memory,
        max_chars=2_000,
        output_dir=tmp_path / "memory",
    )
    compacted_memoryless, events_memoryless = compact_messages(
        memoryless,
        max_chars=2_000,
        output_dir=tmp_path / "memoryless",
    )
    assert len(events_memory) == len(events_memoryless) == 1
    assert events_memory[0]["original_chars"] == events_memoryless[0]["original_chars"]
    assert "externalized" in compacted_memory[-1]["content"]
    assert "externalized" in compacted_memoryless[-1]["content"]
