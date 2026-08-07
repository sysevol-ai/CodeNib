# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from codenib.index.rerank.cross_encoder import (
    QwenRerankerWrapper,
    STCrossEncoderWrapper,
)
from codenib.ops.rerank import CrossEncoderContext


def _install_fake_sentence_transformers(monkeypatch, calls):
    module = ModuleType("sentence_transformers")

    class FakeCrossEncoder:
        def __init__(self, model_name, **kwargs):
            calls.append((model_name, kwargs))

    module.CrossEncoder = FakeCrossEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)


def _install_fake_qwen_dependencies(monkeypatch, tokenizer_calls, model_calls):
    torch_module = ModuleType("torch")
    torch_module.cuda = SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch_module)

    class FakeTokenizer:
        def convert_tokens_to_ids(self, token):
            return {"yes": 1, "no": 2}[token]

        def encode(self, text, *, add_special_tokens):
            return []

    class FakeAutoTokenizer:
        @classmethod
        def from_pretrained(cls, model_name, **kwargs):
            tokenizer_calls.append((model_name, kwargs))
            return FakeTokenizer()

    class FakeModel:
        def to(self, device):
            return self

        def eval(self):
            return self

    class FakeAutoModelForCausalLM:
        @classmethod
        def from_pretrained(cls, model_name, **kwargs):
            model_calls.append((model_name, kwargs))
            return FakeModel()

    transformers_module = ModuleType("transformers")
    transformers_module.AutoTokenizer = FakeAutoTokenizer
    transformers_module.AutoModelForCausalLM = FakeAutoModelForCausalLM
    monkeypatch.setitem(sys.modules, "transformers", transformers_module)


def test_st_reranker_denies_remote_code_by_default(monkeypatch):
    calls = []
    _install_fake_sentence_transformers(monkeypatch, calls)

    STCrossEncoderWrapper("vendor/reranker")

    assert calls == [("vendor/reranker", {"trust_remote_code": False})]


def test_qwen_reranker_denies_remote_code_by_default(monkeypatch):
    tokenizer_calls = []
    model_calls = []
    _install_fake_qwen_dependencies(monkeypatch, tokenizer_calls, model_calls)

    QwenRerankerWrapper("Qwen/Qwen3-Reranker-0.6B")

    assert tokenizer_calls == [
        (
            "Qwen/Qwen3-Reranker-0.6B",
            {"padding_side": "left", "trust_remote_code": False},
        )
    ]
    assert model_calls == [
        (
            "Qwen/Qwen3-Reranker-0.6B",
            {"torch_dtype": "auto", "trust_remote_code": False},
        )
    ]


@pytest.mark.parametrize(
    "revision",
    [None, "main", "a" * 39, "g" * 40],
)
def test_remote_code_requires_immutable_revision(monkeypatch, revision):
    _install_fake_sentence_transformers(monkeypatch, [])

    with pytest.raises(ValueError, match="full 40-character lowercase commit SHA"):
        STCrossEncoderWrapper(
            "vendor/custom-reranker",
            revision=revision,
            trust_remote_code=True,
        )


def test_qwen_remote_code_uses_same_pinned_revision(monkeypatch):
    tokenizer_calls = []
    model_calls = []
    _install_fake_qwen_dependencies(monkeypatch, tokenizer_calls, model_calls)
    revision = "a" * 40

    QwenRerankerWrapper(
        "vendor/custom-reranker",
        revision=revision,
        trust_remote_code=True,
    )

    assert tokenizer_calls[0][1]["revision"] == revision
    assert tokenizer_calls[0][1]["trust_remote_code"] is True
    assert model_calls[0][1]["revision"] == revision
    assert model_calls[0][1]["trust_remote_code"] is True


def test_local_reranker_can_trust_local_code_without_revision(monkeypatch, tmp_path):
    calls = []
    _install_fake_sentence_transformers(monkeypatch, calls)
    model_path = tmp_path / "model"
    model_path.mkdir()

    STCrossEncoderWrapper(str(model_path), trust_remote_code=True)

    assert calls == [(str(model_path), {"trust_remote_code": True})]


def test_lazy_context_forwards_model_loading_policy(monkeypatch):
    from codenib.index.rerank import cross_encoder as module

    wrapper = object()
    calls = []

    def fake_build_reranker(model_name, **kwargs):
        calls.append((model_name, kwargs))
        return wrapper

    monkeypatch.setattr(module, "build_reranker", fake_build_reranker)
    revision = "b" * 40
    context = CrossEncoderContext(
        "vendor/custom-reranker",
        backend="st",
        batch_size=4,
        revision=revision,
        trust_remote_code=True,
    )

    assert context.ensure_wrapper() is wrapper
    assert calls == [
        (
            "vendor/custom-reranker",
            {
                "backend": "st",
                "batch_size": 4,
                "revision": revision,
                "trust_remote_code": True,
            },
        )
    ]


def test_pipeline_forwards_model_loading_policy(monkeypatch, tmp_path):
    from codenib.model import retrieve_rerank_pipeline as module

    wrapper = object()
    calls = []

    def fake_build_reranker(model_name, **kwargs):
        calls.append((model_name, kwargs))
        return wrapper

    monkeypatch.setattr(module, "build_reranker", fake_build_reranker)
    monkeypatch.setattr(
        module.RetrieveRerankPipeline,
        "_initialize_bm25_index",
        lambda self, *, max_k: object(),
    )
    revision = "c" * 40

    pipeline = module.RetrieveRerankPipeline(
        repo_path=str(tmp_path),
        index_path=str(tmp_path / "index"),
        retrieval_mode="sparse",
        rerank_strategy="crossencoder",
        crossencoder_model="vendor/custom-reranker",
        crossencoder_batch_size=2,
        crossencoder_revision=revision,
        crossencoder_trust_remote_code=True,
    )

    assert pipeline.cross_encoder is wrapper
    assert calls == [
        (
            "vendor/custom-reranker",
            {
                "batch_size": 2,
                "revision": revision,
                "trust_remote_code": True,
            },
        )
    ]
