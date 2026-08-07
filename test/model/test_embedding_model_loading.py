# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_embedding_pipeline_does_not_trust_custom_model_by_default(
    monkeypatch,
):
    from codenib.model import embedding_retrieve_pipeline as module

    calls = []

    class FakeStore:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(module, "CodeVectorStore", FakeStore)

    module.EmbeddingRetrievePipeline(
        embedding_model="vendor/standard-model",
    )

    [kwargs] = calls
    assert "trust_remote_code" not in kwargs
    assert "model_kwargs" not in kwargs
    assert "revision" not in kwargs


def test_embedding_pipeline_forwards_explicit_model_policy(monkeypatch):
    from codenib.model import embedding_retrieve_pipeline as module

    calls = []

    class FakeStore:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(module, "CodeVectorStore", FakeStore)
    revision = "c" * 40

    module.EmbeddingRetrievePipeline(
        embedding_model="vendor/custom-model",
        embedding_revision=revision,
        trust_remote_code=True,
    )

    [kwargs] = calls
    assert kwargs["revision"] == revision
    assert kwargs["trust_remote_code"] is True


def test_embedding_pipeline_rejects_huggingface_options_for_remote_provider(
    monkeypatch,
):
    from codenib.model import embedding_retrieve_pipeline as module

    monkeypatch.setattr(module, "CodeVectorStore", object)

    try:
        module.EmbeddingRetrievePipeline(
            embedding_provider="openai",
            embedding_revision="d" * 40,
        )
    except ValueError as exc:
        assert "require the huggingface embedding provider" in str(exc)
    else:
        raise AssertionError("expected provider-specific options to be rejected")


def test_retrieve_rerank_pipeline_preserves_embedding_model_policy():
    from codenib.model.retrieve_rerank_pipeline import RetrieveRerankPipeline

    revision = "f" * 40
    requested = {
        "revision": revision,
        "trust_remote_code": True,
        "model_kwargs": {"device": "cpu"},
        "encode_kwargs": {"normalize_embeddings": True},
    }
    pipeline = object.__new__(RetrieveRerankPipeline)

    prepared = pipeline._prepare_embedding_kwargs("huggingface", requested)

    assert prepared == requested
    assert requested["model_kwargs"] == {"device": "cpu"}


def test_retrieve_rerank_pipeline_preserves_explicit_remote_code_rejection():
    from codenib.model.retrieve_rerank_pipeline import RetrieveRerankPipeline

    pipeline = object.__new__(RetrieveRerankPipeline)

    prepared = pipeline._prepare_embedding_kwargs(
        "huggingface", {"revision": "a" * 40, "trust_remote_code": False}
    )

    assert prepared == {"revision": "a" * 40, "trust_remote_code": False}


@pytest.mark.parametrize("option", ["revision", "trust_remote_code"])
def test_retrieve_rerank_pipeline_rejects_huggingface_options_for_openai(option):
    from codenib.model.retrieve_rerank_pipeline import RetrieveRerankPipeline

    pipeline = object.__new__(RetrieveRerankPipeline)
    value = "a" * 40 if option == "revision" else False

    with pytest.raises(ValueError, match="require the huggingface provider"):
        pipeline._prepare_embedding_kwargs("openai", {option: value})


def test_hybrid_pipeline_forwards_model_policy(monkeypatch, tmp_path):
    from codenib.agent.skills.loader import SkillLoader
    from codenib.model import hybrid_retrieve_pipeline as module

    calls = []

    def fake_build_skill_contexts(**kwargs):
        calls.append(kwargs)
        return {
            "retrieve": SimpleNamespace(
                bm25=object(),
                vector_store=object(),
            )
        }

    monkeypatch.setattr(module, "build_skill_contexts", fake_build_skill_contexts)
    monkeypatch.setattr(SkillLoader, "load_all", lambda *args, **kwargs: [])
    revision = "e" * 40

    module.HybridRetrievePipeline(
        repo_path=str(tmp_path),
        embedding_model="vendor/custom-model",
        embedding_revision=revision,
        trust_remote_code=True,
    )

    [kwargs] = calls
    assert kwargs["embedding_revision"] == revision
    assert kwargs["trust_remote_code"] is True
