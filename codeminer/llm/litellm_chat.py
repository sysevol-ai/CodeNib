# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Lightweight LLM chat wrapper around LiteLLM.

Replaces the LangChain chat model classes (ChatOpenAI, ChatAnthropic, etc.)
with a single thin wrapper that provides the same two-method interface:

    llm = LiteLLMChat(model="gpt-4o", ...)
    structured = llm.with_structured_output(MyPydanticModel)
    result = structured.invoke([human_message("...")])
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

import litellm
from pydantic import BaseModel

from ..log_utils import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Message types (replaces langchain_core.messages.HumanMessage etc.)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatMessage:
    """A single chat message with role and content."""

    role: str  # "system", "user", "assistant"
    content: str


def human_message(content: str) -> ChatMessage:
    """Create a user message."""
    return ChatMessage(role="user", content=content)


def system_message(content: str) -> ChatMessage:
    """Create a system message."""
    return ChatMessage(role="system", content=content)


# ---------------------------------------------------------------------------
# Core wrapper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LiteLLMChat:
    """Thin wrapper around ``litellm.completion``.

    Provides ``.invoke()`` and ``.with_structured_output()`` matching the
    interface that callers already use via LangChain chat models.
    """

    model: str
    temperature: Optional[float] = 0.0
    max_tokens: Optional[int] = 8192
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)

    def invoke(self, messages: List[ChatMessage]) -> str:
        """Send messages and return the assistant content string."""
        response = self._call(messages)
        return response.choices[0].message.content

    def with_structured_output(self, schema: Type[BaseModel]) -> _StructuredLLM:
        """Return a callable that parses LLM responses into *schema*."""
        return _StructuredLLM(chat=self, schema=schema)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call(self, messages: List[ChatMessage], **overrides: Any) -> Any:
        """Build kwargs and call ``litellm.completion``."""
        msg_dicts = [{"role": m.role, "content": m.content} for m in messages]
        return self._call_raw(msg_dicts, **overrides)

    def _call_raw(self, messages: List[Dict[str, Any]], **overrides: Any) -> Any:
        """Like ``_call`` but accepts raw message dicts (for tool-calling flows).

        Accepts two optional control kwargs (not forwarded to litellm):

        - ``usage_tracker``: a :class:`codeminer.llm.usage.UsageTracker`. If
          provided, the response's token usage and cost are recorded.
        - ``usage_turn``: turn index attached to the recorded usage entry.
        """
        import time as _time

        usage_tracker = overrides.pop("usage_tracker", None)
        usage_turn = overrides.pop("usage_turn", None)

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **self.extra_kwargs,
            **overrides,
        }
        if self.api_key is not None:
            kwargs["api_key"] = self.api_key
        if self.api_base is not None:
            kwargs["api_base"] = self.api_base

        # Drop None values so litellm uses its defaults
        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        start = _time.monotonic()
        response = litellm.completion(**kwargs)
        duration_ms = (_time.monotonic() - start) * 1000

        if usage_tracker is not None:
            usage_tracker.record_response(
                response,
                model=self.model,
                duration_ms=duration_ms,
                turn=usage_turn,
            )

        return response


# ---------------------------------------------------------------------------
# Structured output helper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StructuredLLM:
    """Wraps a :class:`LiteLLMChat` to parse responses into a Pydantic model.

    Uses LiteLLM's ``response_format`` parameter which translates to
    provider-native structured output (OpenAI JSON mode, Anthropic tool_use,
    Vertex function calling, etc.).
    """

    chat: LiteLLMChat
    schema: Type[BaseModel]

    def invoke(self, messages: List[ChatMessage]) -> BaseModel:
        """Call the LLM and return a validated Pydantic instance."""
        response = self.chat._call(messages, response_format=self.schema)
        content = response.choices[0].message.content

        try:
            return self.schema.model_validate_json(content)
        except Exception:
            # Fallback: try extracting JSON from markdown code blocks
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
            if match:
                return self.schema.model_validate_json(match.group(1).strip())
            raise
