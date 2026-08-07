# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from codenib.index.embedding.model_policy import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
)
from codenib.index.embedding.vector_store import _HuggingFaceEmbeddingWrapper


def _install_fake_sentence_transformers(monkeypatch, calls):
    module = ModuleType("sentence_transformers")

    class FakeSentenceTransformer:
        def __init__(self, model_name, **kwargs):
            calls.append((model_name, kwargs))

    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)


def test_wrapper_pins_bundled_model_by_default(monkeypatch):
    calls = []
    _install_fake_sentence_transformers(monkeypatch, calls)

    _HuggingFaceEmbeddingWrapper(DEFAULT_EMBEDDING_MODEL)

    assert calls == [
        (
            DEFAULT_EMBEDDING_MODEL,
            {
                "revision": DEFAULT_EMBEDDING_REVISION,
                "trust_remote_code": True,
            },
        )
    ]


def test_wrapper_denies_remote_code_for_arbitrary_model(monkeypatch):
    calls = []
    _install_fake_sentence_transformers(monkeypatch, calls)

    _HuggingFaceEmbeddingWrapper("vendor/standard-model")

    assert calls == [("vendor/standard-model", {"trust_remote_code": False})]


def test_wrapper_accepts_pinned_legacy_model_kwargs(monkeypatch):
    calls = []
    _install_fake_sentence_transformers(monkeypatch, calls)
    revision = "b" * 40

    _HuggingFaceEmbeddingWrapper(
        "vendor/custom-model",
        model_kwargs={
            "device": "cpu",
            "revision": revision,
            "trust_remote_code": True,
        },
    )

    assert calls == [
        (
            "vendor/custom-model",
            {
                "device": "cpu",
                "revision": revision,
                "trust_remote_code": True,
            },
        )
    ]


def test_wrapper_rejects_unpinned_nested_remote_code(monkeypatch):
    _install_fake_sentence_transformers(monkeypatch, [])

    with pytest.raises(ValueError, match="full 40-character lowercase commit SHA"):
        _HuggingFaceEmbeddingWrapper(
            "vendor/mutable-model",
            model_kwargs={"trust_remote_code": True},
        )


def test_wrapper_rejects_conflicting_option_locations(monkeypatch):
    _install_fake_sentence_transformers(monkeypatch, [])

    with pytest.raises(ValueError, match="conflicting trust_remote_code"):
        _HuggingFaceEmbeddingWrapper(
            "vendor/model",
            trust_remote_code=False,
            model_kwargs={"trust_remote_code": True},
        )
