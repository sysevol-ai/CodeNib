# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Common model/ tool-turn mechanics for Guardian's domain agents.

The cycle and investigation agents deliberately retain different state,
budgets, tools, validation, and stopping policies. This module owns the
mechanics that should not differ between them: provider-session lifetime,
assistant-message normalization, tool-argument parsing, transcript updates,
and parallel-safe execution grouping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Sequence

from ..llm import AgentLoopSession


@dataclass(frozen=True)
class ParsedToolCall:
    """Provider-independent view of one requested tool call."""

    id: str
    name: str
    arguments: Mapping[str, object]
    argument_error: str = ""


@dataclass(frozen=True)
class AgentTurn:
    """One model response after it has been appended to the transcript."""

    response: object
    message: object
    tool_calls: Sequence[ParsedToolCall]


def _assistant_message(message: object) -> dict:
    tool_calls = getattr(message, "tool_calls", None) or []
    return {
        "role": "assistant",
        "content": getattr(message, "content", "") or "",
        "tool_calls": [
            {
                "id": str(call.id),
                "type": "function",
                "function": {
                    "name": str(call.function.name),
                    "arguments": call.function.arguments,
                },
            }
            for call in tool_calls
        ],
    }


def _parse_tool_call(call: object) -> ParsedToolCall:
    name = str(call.function.name)
    try:
        arguments = json.loads(call.function.arguments or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return ParsedToolCall(
            id=str(call.id),
            name=name,
            arguments={},
            argument_error=f"malformed tool arguments: {exc}",
        )
    return ParsedToolCall(id=str(call.id), name=name, arguments=arguments)


def execution_batches(
    calls: Sequence[ParsedToolCall],
    parallel_safe_names: Iterable[str],
) -> List[Sequence[ParsedToolCall]]:
    """Group contiguous parallel-safe calls while preserving request order."""

    safe = frozenset(parallel_safe_names)
    batches: List[Sequence[ParsedToolCall]] = []
    cursor = 0
    while cursor < len(calls):
        end = cursor + 1
        if calls[cursor].name in safe:
            while end < len(calls) and calls[end].name in safe:
                end += 1
        batches.append(calls[cursor:end])
        cursor = end
    return batches


class ToolAgentRuntime:
    """Own one model session and the transcript for one domain agent."""

    def __init__(self, llm: object, messages: Sequence[dict]) -> None:
        self.messages = list(messages)
        self._session_manager = AgentLoopSession(llm)
        self._session: Optional[object] = None

    def __enter__(self) -> "ToolAgentRuntime":
        self._session = self._session_manager.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._session_manager.__exit__(exc_type, exc_value, traceback)
        self._session = None

    @property
    def session(self) -> object:
        if self._session is None:
            raise RuntimeError("ToolAgentRuntime must be entered before use")
        return self._session

    def replace_messages(self, messages: Sequence[dict]) -> None:
        """Install a compacted transcript and reset provider-side history."""

        self.messages = list(messages)
        self._session_manager.reset()

    def call_raw(self, messages: Sequence[dict], **kwargs) -> object:
        """Make an off-transcript call, such as context summarization."""

        return self.session._call_raw(list(messages), **kwargs)

    def request(self, *, tools: Sequence[dict], **kwargs) -> AgentTurn:
        """Request one tool turn and append its assistant message."""

        response = self.session._call_raw(
            self.messages,
            tools=list(tools),
            tool_choice="auto",
            **kwargs,
        )
        message = response.choices[0].message
        calls = tuple(
            _parse_tool_call(call)
            for call in (getattr(message, "tool_calls", None) or [])
        )
        self.messages.append(_assistant_message(message))
        return AgentTurn(response=response, message=message, tool_calls=calls)

    def observe(
        self,
        call: ParsedToolCall,
        content: str,
        *,
        is_error: Optional[bool] = None,
    ) -> None:
        """Append one tool observation in provider-compatible transcript form."""

        message = {
            "role": "tool",
            "tool_call_id": call.id,
            "content": content,
        }
        if is_error is not None:
            message["is_error"] = is_error
        self.messages.append(message)
