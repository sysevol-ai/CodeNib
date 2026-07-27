# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Token-aware transcript management shared by Guardian agents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ...agent.history import count_message_tokens


@dataclass
class ContextManager:
    """Token-aware transcript shared by the cycle and investigation agents."""

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
        return self.token_count(include_tools=True) + max(0, int(max_completion_tokens))

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
            output_dir / f"transcript_before_compaction_{len(archives) + 1:04d}.json"
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
