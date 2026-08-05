# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for provider-compatible structured LLM output."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

from codenib.llm.litellm_chat import LiteLLMChat, human_message, system_message


class _ProbeResponse(BaseModel):
    status: str


def _response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(role="assistant", content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _chat(model: str) -> LiteLLMChat:
    return LiteLLMChat(model=model)


def test_deepseek_uses_json_object_with_schema_prompt() -> None:
    chat = _chat("deepseek/deepseek-v4-flash")

    with patch(
        "codenib.llm.litellm_chat.litellm.completion",
        return_value=_response('{"status":"ok"}'),
    ) as mock_completion:
        result = chat.with_structured_output(_ProbeResponse).invoke(
            [human_message("Return status ok.")]
        )

    assert result == _ProbeResponse(status="ok")
    kwargs = mock_completion.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["messages"][0]["role"] == "system"
    assert "JSON Schema" in kwargs["messages"][0]["content"]
    assert '"status"' in kwargs["messages"][0]["content"]
    assert kwargs["messages"][1] == {
        "role": "user",
        "content": "Return status ok.",
    }


def test_json_object_instruction_extends_existing_system_prompt() -> None:
    chat = _chat("deepseek/deepseek-v4-pro")

    with patch(
        "codenib.llm.litellm_chat.litellm.completion",
        return_value=_response('{"status":"ok"}'),
    ) as mock_completion:
        chat.with_structured_output(_ProbeResponse).invoke(
            [system_message("Keep the original policy."), human_message("Answer.")]
        )

    messages = mock_completion.call_args.kwargs["messages"]
    assert len(messages) == 2
    assert messages[0]["content"].startswith("Keep the original policy.\n\n")
    assert "JSON Schema" in messages[0]["content"]


def test_other_providers_keep_pydantic_response_format() -> None:
    chat = _chat("openai/gpt-4o-mini")

    with patch(
        "codenib.llm.litellm_chat.litellm.completion",
        return_value=_response('{"status":"ok"}'),
    ) as mock_completion:
        result = chat.with_structured_output(_ProbeResponse).invoke(
            [human_message("Return status ok.")]
        )

    assert result.status == "ok"
    assert mock_completion.call_args.kwargs["response_format"] is _ProbeResponse


def test_json_object_is_still_validated_against_schema() -> None:
    chat = _chat("deepseek/deepseek-v4-flash")

    with patch(
        "codenib.llm.litellm_chat.litellm.completion",
        return_value=_response('{"unexpected":"value"}'),
    ):
        with pytest.raises(ValidationError):
            chat.with_structured_output(_ProbeResponse).invoke(
                [human_message("Return status ok.")]
            )
