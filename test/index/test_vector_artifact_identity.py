# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codenib.index.embedding.vector_store import (
    CodeVectorStore,
    _OpenAIEmbeddingWrapper,
)


class _Embedding:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_query(self, _text: str) -> list[float]:
        return [0.0] * self.dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


def _store(path, *, fingerprint: str, provider: str = "huggingface"):
    return CodeVectorStore(
        embedding_model="vendor/model",
        embedding_provider=provider,
        dimension=4,
        store_path=str(path),
        embedding=_Embedding(4),
        artifact_metadata={"embedding_fingerprint": fingerprint},
    )


def test_load_rejects_manifest_and_saved_artifact_fingerprint_mismatch(
    tmp_path,
) -> None:
    _store(tmp_path, fingerprint="sha256:first").save()
    reopened = _store(tmp_path, fingerprint="sha256:second")

    with pytest.raises(ValueError, match="does not match manifest"):
        reopened.load()


def test_load_rejects_provider_substitution(tmp_path) -> None:
    _store(tmp_path, fingerprint="sha256:same").save()
    reopened = _store(
        tmp_path,
        fingerprint="sha256:same",
        provider="openai",
    )

    with pytest.raises(ValueError, match="provider mismatch"):
        reopened.load()


def test_load_requires_saved_identity_when_manifest_has_a_fingerprint(tmp_path) -> None:
    reopened = _store(tmp_path, fingerprint="sha256:expected")

    with pytest.raises(ValueError, match="top-level configuration"):
        reopened.load()


def test_openai_wrapper_sends_vector_options_on_embedding_requests() -> None:
    client = MagicMock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[1.0, 2.0])]
    )
    with patch("openai.OpenAI", return_value=client) as factory:
        embedding = _OpenAIEmbeddingWrapper(
            "text-embedding-3-small",
            request_options={"dimensions": 2},
            api_key="runtime-secret",
        )
        assert embedding.embed_query("query") == [1.0, 2.0]

    factory.assert_called_once_with(api_key="runtime-secret")
    client.embeddings.create.assert_called_once_with(
        input=["query"],
        model="text-embedding-3-small",
        dimensions=2,
    )
