# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LLM module for CodeNib."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .decisions import (
        ChoiceAnswer,
        ChoiceQuestion,
        DecisionResult,
        NoulAnswer,
        NoulQuestion,
        OpenRouterDecisions,
        ScoreAnswer,
        ScoreQuestion,
    )
    from .litellm_chat import ChatMessage, LiteLLMChat, RetryConfig

_DECISION_EXPORTS = (
    "ChoiceAnswer",
    "ChoiceQuestion",
    "DecisionResult",
    "NoulAnswer",
    "NoulQuestion",
    "OpenRouterDecisions",
    "ScoreAnswer",
    "ScoreQuestion",
)

__all__ = [
    "ChoiceAnswer",
    "ChoiceQuestion",
    "DecisionResult",
    "NoulAnswer",
    "NoulQuestion",
    "OpenRouterDecisions",
    "ScoreAnswer",
    "ScoreQuestion",
    "ChatMessage",
    "LiteLLMChat",
    "RetryConfig",
    "human_message",
    "is_transient_error",
    "system_message",
]


def __getattr__(name: str) -> Any:
    """Load only the runtime needed by the requested chat or decision symbol."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name in _DECISION_EXPORTS:
        from . import decisions

        value = getattr(decisions, name)
    else:
        from . import litellm_chat

        value = getattr(litellm_chat, name)
    globals()[name] = value
    return value
