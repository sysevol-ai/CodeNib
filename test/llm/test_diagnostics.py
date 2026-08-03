# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codenib.llm.diagnostics import diagnose_model_backend


def _fake_litellm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str,
    missing: list[str],
) -> None:
    from codenib.llm import litellm_chat

    fake = SimpleNamespace(
        get_llm_provider=lambda **_kwargs: ("model", provider, None, None),
        validate_environment=lambda **_kwargs: {
            "keys_in_environment": not missing,
            "missing_keys": missing,
        },
    )
    monkeypatch.setattr(litellm_chat, "litellm", fake)


def test_azure_api_version_option_satisfies_environment_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_litellm(
        monkeypatch,
        provider="azure",
        missing=["AZURE_API_VERSION"],
    )

    report = diagnose_model_backend(
        model="azure/deployment",
        api_base="https://example.openai.azure.com",
        api_key="secret",
        auth_source="AZURE_KEY",
        options={"api_version": "2025-01-01"},
    )

    assert report.configured is True
    assert report.missing_keys == ()
    assert "auth=AZURE_KEY" in report.detail()
    assert "secret" not in report.detail()


def test_vertex_options_satisfy_project_and_location_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_litellm(
        monkeypatch,
        provider="vertex_ai",
        missing=["VERTEXAI_PROJECT", "VERTEXAI_LOCATION"],
    )

    report = diagnose_model_backend(
        model="vertex_ai/gemini-2.5-flash",
        options={
            "vertex_project": "project",
            "vertex_location": "us-central1",
        },
    )

    assert report.configured is True
    assert report.missing_keys == ()
    assert "Application Default Credentials" in report.detail()


@pytest.mark.parametrize(
    "api_base",
    [
        "localhost:8000/v1",
        "http://localhost:8000/v1/chat/completions",
    ],
)
def test_invalid_api_base_fails_before_provider_resolution(api_base: str) -> None:
    report = diagnose_model_backend(
        model="openai/local-model",
        api_base=api_base,
    )

    assert report.configured is False
    assert report.issues


def test_unresolved_model_suggests_a_provider_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.llm import litellm_chat

    monkeypatch.setattr(
        litellm_chat.litellm,
        "get_llm_provider",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("missing provider")),
    )

    report = diagnose_model_backend(
        model="local-model",
        api_base="http://localhost:8000/v1",
    )

    assert report.configured is False
    assert "openai/<served-model>" in report.detail()


def test_unsatisfied_provider_key_remains_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_litellm(
        monkeypatch,
        provider="anthropic",
        missing=["ANTHROPIC_API_KEY"],
    )

    report = diagnose_model_backend(model="anthropic/claude-sonnet")

    assert report.configured is False
    assert report.missing_keys == ("ANTHROPIC_API_KEY",)


def test_openai_compatible_endpoint_defers_optional_auth_to_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_litellm(
        monkeypatch,
        provider="openai",
        missing=["OPENAI_API_KEY"],
    )

    report = diagnose_model_backend(
        model="openai/local-model",
        api_base="http://localhost:8000/v1",
    )

    assert report.configured is True
    assert report.missing_keys == ()
    assert "endpoint authentication" in report.detail()
