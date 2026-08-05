# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from codenib.provider_routes import (
    GITHUB_MODELS_BASE_URL,
    embedding_compatibility_options,
    normalize_endpoint,
    resolve_embedding_artifact_route,
    resolve_inference_route,
    validate_embedding_runtime_options,
)


def test_github_models_route_discovers_action_token_without_storing_it() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="github-models",
        model="openai/text-embedding-3-small",
        dimension=1536,
        environ={"GITHUB_TOKEN": "action-secret"},
    )

    assert route.provider == "github_models"
    assert route.endpoint == GITHUB_MODELS_BASE_URL
    assert route.credential_env == "GITHUB_TOKEN"
    assert route.client_kwargs({"GITHUB_TOKEN": "action-secret"}) == {
        "base_url": GITHUB_MODELS_BASE_URL,
        "api_key": "action-secret",
    }
    serialized = json.dumps(route.public_identity(), sort_keys=True)
    assert "action-secret" not in serialized
    assert "GITHUB_TOKEN" not in serialized


def test_github_models_route_uses_explicit_credential_then_gh_token() -> None:
    explicit = resolve_inference_route(
        operation="chat",
        provider="github_models",
        model="openai/gpt-4.1",
        credential_env="MODELS_TOKEN",
        environ={"GITHUB_TOKEN": "action", "MODELS_TOKEN": "explicit"},
    )
    fallback = resolve_inference_route(
        operation="chat",
        provider="github_models",
        model="openai/gpt-4.1",
        environ={"GH_TOKEN": "cli-token"},
    )

    assert explicit.client_model == "openai/openai/gpt-4.1"
    assert explicit.credential_env == "MODELS_TOKEN"
    assert explicit.credential({"MODELS_TOKEN": "explicit"}) == "explicit"
    assert fallback.credential_env == "GH_TOKEN"


def test_github_models_route_reports_missing_credentials_without_token_data() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="github_models",
        model="openai/text-embedding-3-small",
        dimension=1536,
        environ={},
    )

    with pytest.raises(ValueError, match="GITHUB_TOKEN") as error:
        route.client_kwargs({})
    assert "Bearer" not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        "models.github.ai/inference",
        "https://token@models.github.ai/inference",
        "https://models.github.ai/inference?api_key=secret",
        "https://models.github.ai/inference#fragment",
        "https://models.github.ai/inference/../other",
        "https://models.github.ai/inference/embeddings",
        "https://models.github.ai/inference/%65mbeddings",
    ],
)
def test_endpoint_normalization_rejects_unsafe_or_operation_urls(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_endpoint(value)


def test_endpoint_normalization_is_stable() -> None:
    assert (
        normalize_endpoint("HTTPS://MODELS.GITHUB.AI/inference/")
        == GITHUB_MODELS_BASE_URL
    )


def test_embedding_fingerprint_tracks_vector_semantics_not_runtime_knobs() -> None:
    base = dict(
        operation="embeddings",
        provider="huggingface",
        model="model/revision",
        dimension=768,
    )
    route = resolve_inference_route(
        **base,
        compatibility_options={
            "revision": "abc",
            "max_seq_length": 2048,
            "encode_kwargs": {
                "batch_size": 4,
                "normalize_embeddings": True,
            },
        },
    )
    other_batch = resolve_inference_route(
        **base,
        compatibility_options={
            "revision": "abc",
            "max_seq_length": 2048,
            "encode_kwargs": {
                "batch_size": 64,
                "normalize_embeddings": True,
            },
        },
    )
    different_prompt = resolve_inference_route(
        **base,
        compatibility_options={
            "revision": "abc",
            "max_seq_length": 2048,
            "query_prompt": "Represent this query: ",
            "encode_kwargs": {"normalize_embeddings": True},
        },
    )

    assert route.compatibility_fingerprint == other_batch.compatibility_fingerprint
    assert route.compatibility_fingerprint != different_prompt.compatibility_fingerprint
    assert route.compatibility_options["encode_kwargs"] == {
        "normalize_embeddings": True
    }


def test_embedding_fingerprint_tracks_execution_device() -> None:
    cpu = resolve_inference_route(
        operation="embeddings",
        provider="huggingface",
        model="vendor/model",
        dimension=768,
        compatibility_options={"encode_kwargs": {"device": "cpu"}},
    )
    cuda = resolve_inference_route(
        operation="embeddings",
        provider="huggingface",
        model="vendor/model",
        dimension=768,
        compatibility_options={"encode_kwargs": {"device": "cuda"}},
    )

    assert cpu.compatibility_fingerprint != cuda.compatibility_fingerprint


@pytest.mark.parametrize(
    "options",
    [
        {"api_key": "secret"},
        {"headers": {"Authorization": "Bearer secret"}},
        {"model_kwargs": {"access_token": "secret"}},
        {"fallbacks": [{"api_key": "secret"}]},
        {"base_url": "https://example.test/v1"},
    ],
)
def test_embedding_artifact_options_reject_credentials_and_endpoints(options) -> None:
    with pytest.raises(ValueError):
        embedding_compatibility_options(options)


def test_chat_compatibility_options_reject_credentials() -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        resolve_inference_route(
            operation="chat",
            provider="openai",
            model="gpt-4.1",
            compatibility_options={"headers": {"Authorization": "Bearer secret"}},
        )

    with pytest.raises(ValueError, match="dedicated endpoint"):
        resolve_inference_route(
            operation="chat",
            provider="openai",
            model="gpt-4.1",
            compatibility_options={"api_base": "https://example.test/v1"},
        )


def test_openai_custom_endpoint_can_be_intentionally_unauthenticated() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="served-embedding",
        endpoint="http://localhost:8080/v1/",
        dimension=1024,
        environ={},
    )

    assert route.client_kwargs({}) == {"base_url": "http://localhost:8080/v1"}


def test_openai_embedding_dimensions_are_request_options() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="openai",
        model="text-embedding-3-small",
        dimension=512,
        compatibility_options={"dimensions": 512},
    )

    assert route.embedding_backend_kwargs() == {"request_options": {"dimensions": 512}}
    with pytest.raises(ValueError, match="must equal"):
        resolve_inference_route(
            operation="embeddings",
            provider="openai",
            model="text-embedding-3-small",
            dimension=512,
            compatibility_options={"dimensions": 256},
        )


def test_huggingface_route_rejects_remote_configuration() -> None:
    with pytest.raises(ValueError, match="local Hugging Face"):
        resolve_inference_route(
            operation="embeddings",
            provider="huggingface",
            model="nomic-ai/CodeRankEmbed",
            endpoint="https://example.test/v1",
            dimension=768,
        )


def test_runtime_options_are_provider_specific_and_cannot_override_semantics() -> None:
    assert validate_embedding_runtime_options(
        {"api-key": "secret", "max-retries": 2},
        provider="github_models",
    ) == {"api_key": "secret", "max_retries": 2}
    assert validate_embedding_runtime_options(
        {"encode_kwargs": {"batch-size": 8}},
        provider="huggingface",
    ) == {"encode_kwargs": {"batch_size": 8}}

    with pytest.raises(ValueError, match="vector compatibility"):
        validate_embedding_runtime_options(
            {"query_prompt": "changed"},
            provider="huggingface",
        )
    with pytest.raises(ValueError, match="vector semantics"):
        validate_embedding_runtime_options(
            {"encode_kwargs": {"normalize_embeddings": False}},
            provider="huggingface",
        )


def test_schema_v3_artifact_route_round_trips_and_rejects_drift() -> None:
    route = resolve_inference_route(
        operation="embeddings",
        provider="github_models",
        model="openai/text-embedding-3-small",
        dimension=1536,
        environ={},
    )
    artifact = {
        "builder_schema": 3,
        "embedding_model": route.model,
        "embedding_provider": route.provider,
        "embedding_dimension": route.dimension,
        "dimension": route.dimension,
        "embedding_endpoint": route.endpoint,
        "embedding_kwargs": route.compatibility_options,
        "embedding_route": route.public_identity(),
        "embedding_fingerprint": route.compatibility_fingerprint,
    }

    reopened = resolve_embedding_artifact_route(artifact, environ={})
    assert reopened.public_identity() == route.public_identity()

    drifted = json.loads(json.dumps(artifact))
    drifted["embedding_route"]["model"] = "openai/text-embedding-3-large"
    with pytest.raises(ValueError, match="disagrees|fingerprint"):
        resolve_embedding_artifact_route(drifted, environ={})


def test_legacy_artifact_route_is_supported_but_private_kwargs_are_not() -> None:
    route = resolve_embedding_artifact_route(
        {
            "builder_schema": 2,
            "embedding_model": "vendor/model",
            "embedding_provider": "huggingface",
            "embedding_dimension": 384,
            "embedding_kwargs": {
                "revision": "immutable",
                "encode_kwargs": {"batch_size": 16},
            },
        }
    )
    assert route.model == "vendor/model"
    assert route.compatibility_options == {"revision": "immutable"}

    with pytest.raises(ValueError, match="credentials"):
        resolve_embedding_artifact_route(
            {
                "embedding_model": "served-model",
                "embedding_provider": "openai",
                "embedding_dimension": 384,
                "embedding_kwargs": {"api_key": "persisted-secret"},
            }
        )
