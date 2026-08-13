# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the real-model verifier.

The ``best_linear_path`` flattening logic is pure and always runs. The
losslessness check needs a GPU + the ``bench`` extra + a target model, so it is
marked ``slow`` and skips unless ``CODENIB_SERVE_TEST_MODEL`` points at a loadable
checkpoint.
"""

import os
import sys
import types
from contextlib import nullcontext

import pytest

from codenib.serving.server.real_model import (
    RealModelVerifier,
    _load_lm,
    best_linear_path,
)
from codenib.serving.types import DraftNode, DraftTree


def test_best_linear_path_on_linear_tree():
    tree = DraftTree()
    tree.add_sequence([4, 5, 6])
    assert best_linear_path(tree) == [4, 5, 6]


def test_best_linear_path_empty_tree():
    assert best_linear_path(DraftTree()) == []


def test_best_linear_path_picks_highest_scored_branch():
    # Two branches diverge at the first token; the higher-scored one wins.
    low = DraftNode(token=1, score=0.2)
    low.children.append(DraftNode(token=2, score=0.2))
    high = DraftNode(token=9, score=0.9)
    high.children.append(DraftNode(token=8, score=0.9))
    tree = DraftTree()
    tree.root.children.extend([low, high])
    assert best_linear_path(tree) == [9, 8]


class _CallableModelStub:
    """Model stand-in: records nothing, answers a text forward with logits."""

    def __init__(self, logits_ndim=3):
        self.config = types.SimpleNamespace(bos_token_id=1)
        self._ndim = logits_ndim

    def __call__(self, **kwargs):
        return types.SimpleNamespace(logits=types.SimpleNamespace(ndim=self._ndim))


def _fake_transformers(monkeypatch, *, model_type, vlm_result=None):
    """Install stand-in ``transformers`` modules for auto-class resolution.

    ``AutoConfig`` reports ``model_type`` for every model name; the causal-LM
    mapping contains only ``"mapped_causal"``. ``AutoModelForImageTextToText``
    returns ``vlm_result`` (default: a validation-passing model stub).
    """
    mod = types.ModuleType("transformers")

    class _CausalLM:
        @staticmethod
        def from_pretrained(name, torch_dtype=None, **kwargs):
            return f"causal:{name}:{torch_dtype}"

    class _AutoConfig:
        @staticmethod
        def from_pretrained(name, **kwargs):
            return types.SimpleNamespace(model_type=model_type)

    class _ImageTextToText:
        @staticmethod
        def from_pretrained(name, torch_dtype=None, **kwargs):
            return vlm_result if vlm_result is not None else _CallableModelStub()

    mod.AutoModelForCausalLM = _CausalLM
    mod.AutoConfig = _AutoConfig
    mod.AutoModelForImageTextToText = _ImageTextToText

    auto_mod = types.ModuleType("transformers.models.auto.modeling_auto")
    auto_mod.MODEL_FOR_CAUSAL_LM_MAPPING_NAMES = {"mapped_causal": "FakeForCausalLM"}
    monkeypatch.setitem(sys.modules, "transformers", mod)
    monkeypatch.setitem(sys.modules, "transformers.models.auto.modeling_auto", auto_mod)

    fake_torch = types.ModuleType("torch")
    fake_torch.long = object()
    fake_torch.tensor = lambda *args, **kwargs: object()
    fake_torch.no_grad = nullcontext
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    return mod


def test_load_lm_uses_causal_lm_for_mapped_model_type(monkeypatch):
    # Every target we run — including Qwen3.5-4B, whose Qwen3_5ForCausalLM is in
    # the causal-LM auto-mapping on transformers >= 5.2 — resolves this way, with
    # no validation forward.
    _fake_transformers(monkeypatch, model_type="mapped_causal")
    assert (
        _load_lm("some/causal-model", dtype="bf16") == "causal:some/causal-model:bf16"
    )


def test_load_lm_resolves_registry_model_type_and_validates(monkeypatch):
    # muse_glimmer is outside the causal-LM mapping but allowlisted in
    # VERIFIED_NON_CAUSAL_MODEL_TYPES; it loads via AutoModelForImageTextToText
    # and must pass the one-token text-forward validation.
    _fake_transformers(monkeypatch, model_type="muse_glimmer")
    model = _load_lm("meta-models/Muse-Glimmer-30B", dtype=None)
    assert isinstance(model, _CallableModelStub)


def test_load_lm_registry_load_failing_validation_raises(monkeypatch):
    # A registry entry whose checkpoint does not emit [batch, seq, vocab] logits
    # from a text-only forward is rejected at load time, not at verify time.
    _fake_transformers(
        monkeypatch,
        model_type="muse_glimmer",
        vlm_result=_CallableModelStub(logits_ndim=2),
    )
    with pytest.raises(RuntimeError, match="logits"):
        _load_lm("meta-models/Muse-Glimmer-30B", dtype=None)


def test_load_lm_unknown_model_type_fails_closed(monkeypatch):
    # No blind fallback: a model_type outside both the causal-LM mapping and the
    # registry raises with the remediation named, instead of guessing a class
    # that might load but verify against the wrong logits.
    _fake_transformers(monkeypatch, model_type="novel_arch")
    with pytest.raises(RuntimeError, match="VERIFIED_NON_CAUSAL_MODEL_TYPES"):
        _load_lm("x/y", dtype=None)


def test_load_lm_honours_explicit_model_class(monkeypatch):
    # Explicit override wins without touching resolution or validation — the
    # caller asserts the class is right (this is also how tests inject stubs).
    _fake_transformers(monkeypatch, model_type="novel_arch")

    class _Forced:
        @staticmethod
        def from_pretrained(name, torch_dtype=None, **kwargs):
            return f"forced:{name}:{torch_dtype}"

    assert _load_lm("x/y", dtype="fp32", model_class=_Forced) == "forced:x/y:fp32"


def test_real_model_verifier_stops_before_drafted_eos(monkeypatch) -> None:
    class _Row:
        def __init__(self, prediction: int) -> None:
            self.prediction = prediction

        def argmax(self, _axis: int) -> int:
            return self.prediction

    class _Model:
        def __call__(self, _input_ids):
            # With one context token these rows predict draft tokens 4, EOS,
            # then a post-EOS token that must never be consumed as a bonus.
            return types.SimpleNamespace(logits=[[_Row(4), _Row(0), _Row(42)]])

    fake_torch = types.ModuleType("torch")
    fake_torch.long = object()
    fake_torch.tensor = lambda *args, **kwargs: object()
    fake_torch.no_grad = nullcontext
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    verifier = RealModelVerifier.__new__(RealModelVerifier)
    verifier.device = "cpu"
    verifier.model = _Model()
    verifier._eos = {0}
    tree = DraftTree()
    tree.add_sequence([4, 0, 42], source="test")

    result = verifier.verify([1], tree)

    assert result.accepted == [4]
    assert result.bonus is None


@pytest.mark.slow
def test_real_model_verifier_is_lossless_vs_vanilla_ar():
    """Drafted greedy output must be byte-identical to vanilla AR greedy output."""
    model_name = os.environ.get("CODENIB_SERVE_TEST_MODEL")
    if not model_name:
        pytest.skip("set CODENIB_SERVE_TEST_MODEL to a loadable checkpoint to run")
    pytest.importorskip("torch")
    pytest.importorskip("transformers")

    from codenib.serving.drafter.copy import CopyDrafter
    from codenib.serving.server.real_model import RealModelVerifier
    from codenib.serving.server.worker import SpeculativeConfig, SpeculativeServer

    verifier = RealModelVerifier(model_name)
    prompt = verifier.tokenizer("def add(a, b):", return_tensors=None)["input_ids"]

    config = SpeculativeConfig(max_draft_tokens=8)
    ar = SpeculativeServer(drafters=[], config=config)
    spec = SpeculativeServer(
        drafters=[CopyDrafter(min_match=2, max_match=16, max_draft=8)],
        config=config,
    )

    ar_out = ar.run(prompt, verifier, max_new_tokens=48)
    spec_out = spec.run(prompt, verifier, max_new_tokens=48)

    # Same tokens (lossless), and speculation never costs extra forward passes.
    assert spec_out.tokens == ar_out.tokens
    assert spec_out.forward_passes <= ar_out.forward_passes
