# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures shared by agent tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_VERTEX_TEST_MODELS = (
    pytest.param("vertex_ai/gemini-2.5-flash", id="gemini-2.5-flash"),
    pytest.param(
        "vertex_ai/claude-haiku-4-5@20251001",
        id="claude-haiku-4.5",
    ),
)


@pytest.fixture(scope="session")
def require_vertex_credentials() -> Path:
    """Require an explicit, readable ADC file before making billed calls."""
    raw_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw_path:
        pytest.skip(
            "Vertex AI tests require GOOGLE_APPLICATION_CREDENTIALS to point "
            "to an application-default credentials file"
        )

    credentials_path = Path(raw_path).expanduser()
    if not credentials_path.is_file() or credentials_path.stat().st_size == 0:
        pytest.fail(
            f"GOOGLE_APPLICATION_CREDENTIALS is missing or empty: {credentials_path}"
        )
    return credentials_path


@pytest.fixture(params=_VERTEX_TEST_MODELS)
def vertex_model(request: pytest.FixtureRequest) -> str:
    """Return one revision-pinned, actively supported Vertex smoke model."""
    return str(request.param)
