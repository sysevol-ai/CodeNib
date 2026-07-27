# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Shared mechanics for model-owned Guardian loops.

L2 and L3 intentionally keep separate prompts, tools, state, and stopping
policies.  This module only owns the conversation mechanics that both loops
need: transport-session lifetime, token-aware transcript management, and a
typed terminal outcome.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ...agent.history import count_message_tokens


class AgentLoopSession(AbstractContextManager):
    """Own exactly one transport session for exactly one model agent loop."""

    def __init__(self, llm: object) -> None:
        self.llm = llm
        self.session: object = llm

    def __enter__(self) -> object:
        # Inspect the class so mocks/proxies cannot fabricate this attribute.
        factory = getattr(type(self.llm), "start_agent_loop", None)
        self.session = factory(self.llm) if callable(factory) else self.llm
        return self.session

    def reset(self) -> None:
        """Reset provider-side history after local transcript compaction."""

        reset = getattr(self.session, "reset", None)
        if callable(reset):
            reset()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.session is not self.llm:
            close = getattr(self.session, "close", None)
            if callable(close):
                close()


@dataclass(frozen=True)
class LoopOutcome:
    """Mechanical reason a model-owned loop stopped."""

    status: str
    reasoning: str = ""


@dataclass
class ContextManager:
    """Token-aware transcript shared by L2 and each independent L3 loop."""

    messages: List[dict]
    max_tokens: int
    reserve_tokens: int
    model: Optional[str] = None
    tools: Optional[List[dict]] = None

    @property
    def usable_tokens(self) -> int:
        return max(1, self.max_tokens - max(0, self.reserve_tokens))

    def token_count(self, *, include_tools: bool = False) -> int:
        messages = list(self.messages)
        if include_tools and self.tools:
            messages.append(
                {
                    "role": "system",
                    "content": "Tool schemas:\n"
                    + json.dumps(self.tools, sort_keys=True, default=str),
                }
            )
        return count_message_tokens(messages, model=self.model)

    def needs_compaction(self) -> bool:
        return self.token_count(include_tools=True) > self.usable_tokens

    def estimated_call_tokens(self, max_completion_tokens: int) -> int:
        return self.token_count(include_tools=True) + max(
            0, int(max_completion_tokens)
        )

    def summarization_messages(
        self, prompt: str, canonical_snapshot: object
    ) -> List[dict]:
        return [
            *self.messages,
            {
                "role": "user",
                "content": (
                    f"{prompt}\nCanonical state at compaction:\n"
                    f"{json.dumps(canonical_snapshot, sort_keys=True, default=str)}"
                ),
            },
        ]

    def compact(
        self,
        *,
        summary: str,
        canonical_snapshot: object,
        output_dir: Path,
        memory_heading: str,
        keep_recent_turns: int = 0,
    ) -> List[dict]:
        """Archive old history and replace it with coherent working memory."""

        if len(self.messages) <= 2:
            return []
        output_dir.mkdir(parents=True, exist_ok=True)
        archives = sorted(output_dir.glob("transcript_before_compaction_*.json"))
        target = (
            output_dir
            / f"transcript_before_compaction_{len(archives) + 1:04d}.json"
        )
        target.write_text(
            json.dumps(self.messages, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        recent = _recent_complete_turns(self.messages, keep_recent_turns)
        before = len(self.messages)
        self.messages = [
            self.messages[0],
            self.messages[1],
            {
                "role": "user",
                "content": (
                    f"=== {memory_heading} ===\n"
                    f"{summary.strip()}\n\n"
                    "Canonical state:\n"
                    f"{json.dumps(canonical_snapshot, sort_keys=True, default=str)}\n"
                    f"=== END {memory_heading} ==="
                ),
            },
            *recent,
        ]
        return [
            {
                "event": "compaction",
                "strategy": "structured_summary",
                "archive": target.name,
                "messages_before": before,
                "messages_after": len(self.messages),
                "recent_turns_retained": keep_recent_turns,
                "summary_chars": len(summary),
            }
        ]


def _recent_complete_turns(messages: List[dict], keep_turns: int) -> List[dict]:
    if keep_turns <= 0:
        return []
    assistant_positions = [
        index
        for index, message in enumerate(messages[2:], start=2)
        if message.get("role") == "assistant"
    ]
    if not assistant_positions:
        return []
    return list(messages[assistant_positions[-keep_turns] :])
