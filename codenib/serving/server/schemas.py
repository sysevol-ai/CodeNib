# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible request/response models for the serving endpoint.

A minimal subset of the OpenAI Chat Completions schema — enough for a code agent
(or LiteLLM in front of one) to call CodeNib serving exactly as it would call OpenAI.
Sampling fields (``temperature``/``top_p``) are accepted for wire-compatibility
but v1 only serves greedy; the endpoint rejects non-greedy values (see
:mod:`codenib.serving.server.api`).
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# These limits are enforced before model inference.  The byte-level HTTP cap
# lives in ``server.api``; the schema limits keep JSON that fits under that cap
# from expanding into an unbounded number of messages or tokenizer work.
MAX_CHAT_MESSAGES = 128
MAX_MESSAGE_CHARACTERS = 262_144
MAX_PROMPT_CHARACTERS = 262_144
MAX_COMPLETION_TOKENS = 4_096
MAX_MODEL_NAME_CHARACTERS = 512


class _StrictWireModel(BaseModel):
    """Reject fields the endpoint does not implement instead of ignoring them."""

    model_config = ConfigDict(extra="forbid")


class ChatMessage(_StrictWireModel):
    """A single chat message (``role`` is typically system/user/assistant)."""

    role: Literal["system", "developer", "user", "assistant"]
    content: str = Field(max_length=MAX_MESSAGE_CHARACTERS, strict=True)


class ChatCompletionRequest(_StrictWireModel):
    """The subset of ``POST /v1/chat/completions`` fields CodeNib serving honors."""

    model: str = Field(
        min_length=1,
        max_length=MAX_MODEL_NAME_CHARACTERS,
        strict=True,
    )
    messages: List[ChatMessage] = Field(
        min_length=1,
        max_length=MAX_CHAT_MESSAGES,
    )
    max_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        le=MAX_COMPLETION_TOKENS,
        strict=True,
    )
    max_completion_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        le=MAX_COMPLETION_TOKENS,
        strict=True,
    )
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    stream: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _bound_prompt_characters(self) -> "ChatCompletionRequest":
        total = sum(len(message.content) for message in self.messages)
        if total > MAX_PROMPT_CHARACTERS:
            raise ValueError(
                f"messages contain more than {MAX_PROMPT_CHARACTERS} characters"
            )
        return self


class Choice(_StrictWireModel):
    """One non-streamed completion choice."""

    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = None


class Usage(_StrictWireModel):
    """Token accounting for a completion."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(_StrictWireModel):
    """A non-streamed ``chat.completion`` response."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: List[Choice]
    usage: Usage


class DeltaMessage(_StrictWireModel):
    """The incremental payload of a streaming chunk."""

    role: Optional[str] = None
    content: Optional[str] = None


class ChunkChoice(_StrictWireModel):
    """One streamed choice delta."""

    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None


class ChatCompletionChunk(_StrictWireModel):
    """A single ``chat.completion.chunk`` streamed over SSE."""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChunkChoice]


class ModelCard(_StrictWireModel):
    """One entry in ``GET /v1/models``."""

    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "codenib"


class ModelList(_StrictWireModel):
    """The ``GET /v1/models`` response."""

    object: Literal["list"] = "list"
    data: List[ModelCard]
