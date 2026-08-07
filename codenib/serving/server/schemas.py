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

from pydantic import BaseModel


class ChatMessage(BaseModel):
    """A single chat message (``role`` is typically system/user/assistant)."""

    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    """The subset of ``POST /v1/chat/completions`` fields CodeNib serving honors."""

    model: str
    messages: List[ChatMessage]
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stream: bool = False


class Choice(BaseModel):
    """One non-streamed completion choice."""

    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = None


class Usage(BaseModel):
    """Token accounting for a completion."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    """A non-streamed ``chat.completion`` response."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: List[Choice]
    usage: Usage


class DeltaMessage(BaseModel):
    """The incremental payload of a streaming chunk."""

    role: Optional[str] = None
    content: Optional[str] = None


class ChunkChoice(BaseModel):
    """One streamed choice delta."""

    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    """A single ``chat.completion.chunk`` streamed over SSE."""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChunkChoice]


class ModelCard(BaseModel):
    """One entry in ``GET /v1/models``."""

    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "codenib"


class ModelList(BaseModel):
    """The ``GET /v1/models`` response."""

    object: Literal["list"] = "list"
    data: List[ModelCard]
