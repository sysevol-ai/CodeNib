#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Live Vertex AI generation checks for LiteLLMChat."""

import pytest

from codenib.llm.litellm_chat import LiteLLMChat, human_message

pytestmark = pytest.mark.slow


def test_vertex_ai_generation(require_vertex_credentials, vertex_model):
    """A configured provider must return non-empty text or fail the slow tier."""
    llm = LiteLLMChat(model=vertex_model, temperature=0.0, max_tokens=256)

    response = llm.invoke(
        [human_message("What is machine learning? Give a brief explanation.")]
    )

    assert response
    assert response.strip()
