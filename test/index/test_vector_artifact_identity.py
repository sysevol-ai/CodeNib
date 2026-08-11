# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codenib.index.embedding.artifact_integrity import capture_authenticated_vector_view
from codenib.index.embedding.vector_store import (
    CodeVectorStore,
    _OpenAIEmbeddingWrapper,
    _read_authenticated_faiss,
)
from codenib.native_index_authorization import _mint_trusted_local_admin_authorization


class _Embedding:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_query(self, _text: str) -> list[float]:
        return [0.0] * self.dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


def test_faiss_callback_caps_oversized_native_read_requests(monkeypatch) -> None:
    payload_size = 8 * 1024 * 1024 + 17

    class _Snapshot:
        def __init__(self) -> None:
            self.record = SimpleNamespace(size=payload_size)
            self.offset = 0
            self.requests: list[int] = []

        def read(self, size: int) -> bytes:
            self.requests.append(size)
            remaining = payload_size - self.offset
            consumed = min(size, remaining)
            self.offset += consumed
            return b"x" * consumed

    snapshot = _Snapshot()
    view = SimpleNamespace(
        authenticated_snapshot=lambda _relative: nullcontext(
            (snapshot, snapshot.record)
        )
    )
    observed: list[int] = []
    sentinel = object()

    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.faiss.PyCallbackIOReader",
        lambda callback: callback,
    )

    def read_index(callback):
        while block := callback(1 << 60):
            observed.append(len(block))
        return sentinel

    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.faiss.read_index",
        read_index,
    )

    assert _read_authenticated_faiss(view, "l2/index.faiss") is sentinel
    assert observed == [8 * 1024 * 1024, 17]
    assert max(snapshot.requests) == 8 * 1024 * 1024


def _store(
    path,
    *,
    fingerprint: str,
    provider: str = "huggingface",
    revision: str | None = None,
):
    embedding_kwargs = {"revision": revision} if revision is not None else {}
    return CodeVectorStore(
        embedding_model="vendor/model",
        embedding_provider=provider,
        dimension=4,
        store_path=str(path),
        embedding=_Embedding(4),
        artifact_metadata={"embedding_fingerprint": fingerprint},
        **embedding_kwargs,
    )


def _authorization(path, store):
    with capture_authenticated_vector_view(path) as view:
        return _mint_trusted_local_admin_authorization(
            view.ownership,
            view_type="vector",
            semantic_contract=store.artifact_metadata,
            evidence=(
                "vector-artifact-test-local-admin",
                "captured-vector-tree-subject",
            ),
        )


def test_load_denies_missing_authorization_before_tree_capture(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, fingerprint="sha256:expected")
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: pytest.fail("denied native load must not capture the tree"),
    )

    with pytest.raises(ValueError, match="requires external authorization"):
        store.load()


def test_load_denies_foreign_pid_authorization_before_tree_capture(
    tmp_path,
    monkeypatch,
) -> None:
    import codenib.native_index_authorization as authorization_module

    store = _store(tmp_path, fingerprint="sha256:expected")
    authorization = _authorization(tmp_path, store)
    monkeypatch.setattr(
        authorization_module.os,
        "getpid",
        lambda: authorization.process_id + 1,
    )
    monkeypatch.setattr(
        "codenib.index.embedding.vector_store.capture_authenticated_vector_view",
        lambda _path: pytest.fail("foreign-PID load must not capture the tree"),
    )

    with pytest.raises(ValueError, match="another process"):
        store.load(native_index_authorization=authorization)


def test_load_rejects_manifest_and_saved_artifact_fingerprint_mismatch(
    tmp_path,
) -> None:
    _store(tmp_path, fingerprint="sha256:first").save()
    reopened = _store(tmp_path, fingerprint="sha256:second")

    with pytest.raises(ValueError, match="does not match manifest"):
        reopened.load(native_index_authorization=_authorization(tmp_path, reopened))


def test_load_rejects_provider_substitution(tmp_path) -> None:
    _store(tmp_path, fingerprint="sha256:same").save()
    reopened = _store(
        tmp_path,
        fingerprint="sha256:same",
        provider="openai",
    )

    with pytest.raises(ValueError, match="provider mismatch"):
        reopened.load(native_index_authorization=_authorization(tmp_path, reopened))


def test_load_rejects_embedding_revision_substitution(tmp_path) -> None:
    _store(tmp_path, fingerprint="sha256:same", revision="a" * 40).save()
    reopened = _store(
        tmp_path,
        fingerprint="sha256:same",
        revision="b" * 40,
    )

    with pytest.raises(ValueError, match="embedding revision mismatch"):
        reopened.load(native_index_authorization=_authorization(tmp_path, reopened))


@pytest.mark.parametrize("option", ["revision", "trust_remote_code"])
def test_remote_provider_rejects_huggingface_model_options(tmp_path, option) -> None:
    value = "a" * 40 if option == "revision" else False

    with pytest.raises(ValueError, match="require provider='huggingface'"):
        CodeVectorStore(
            embedding_model="text-embedding-3-small",
            embedding_provider="openai",
            dimension=4,
            store_path=str(tmp_path),
            embedding=_Embedding(4),
            **{option: value},
        )


def test_load_requires_saved_identity_when_manifest_has_a_fingerprint(tmp_path) -> None:
    reopened = _store(tmp_path, fingerprint="sha256:expected")

    with pytest.raises(ValueError, match="top-level configuration"):
        reopened.load(native_index_authorization=_authorization(tmp_path, reopened))


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
