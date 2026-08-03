# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""LLM module for CodeNib."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .litellm_chat import ChatMessage, LiteLLMChat, RetryConfig

__all__ = [
    "ChatMessage",
    "LiteLLMChat",
    "RetryConfig",
    "human_message",
    "is_transient_error",
    "system_message",
]


def __getattr__(name: str) -> Any:
    """Load the optional LiteLLM runtime only when an LLM symbol is requested."""

    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import litellm_chat

    value = getattr(litellm_chat, name)
    globals()[name] = value
    return value
