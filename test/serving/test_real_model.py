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

import pytest

from codenib.serving.server.real_model import _load_lm, best_linear_path
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


def _fake_transformers(*, causal_raises: bool):
    """A stand-in ``transformers`` module recording which auto-class was used.

    ``AutoModelForCausalLM`` raises ``ValueError`` (mimicking an unmapped
    architecture) when ``causal_raises``, else returns a sentinel string tagging
    the class that loaded.
    """
    mod = types.ModuleType("transformers")

    class _CausalLM:
        @staticmethod
        def from_pretrained(name, dtype=None):
            if causal_raises:
                raise ValueError("architecture not mapped for causal LM")
            return f"causal:{name}"

    mod.AutoModelForCausalLM = _CausalLM
    return mod


def test_load_lm_uses_causal_lm_by_default(monkeypatch):
    # Every target we run — including Qwen3.5-4B, whose Qwen3_5ForCausalLM is in
    # the causal-LM auto-mapping on transformers >= 5.2 — loads this way.
    monkeypatch.setitem(
        sys.modules, "transformers", _fake_transformers(causal_raises=False)
    )
    assert _load_lm("some/causal-model", dtype=None) == "causal:some/causal-model"


def test_load_lm_does_not_silently_fall_back(monkeypatch):
    # If the causal-LM mapping genuinely rejects a model, the error propagates —
    # there is no automatic multimodal fallback; callers pass an explicit
    # model_class instead (see test below).
    monkeypatch.setitem(
        sys.modules, "transformers", _fake_transformers(causal_raises=True)
    )
    with pytest.raises(ValueError):
        _load_lm("x/y", dtype=None)


def test_load_lm_honours_explicit_model_class(monkeypatch):
    # Explicit override wins over the causal-LM default and is used even when the
    # causal-LM mapping would have rejected the model.
    monkeypatch.setitem(
        sys.modules, "transformers", _fake_transformers(causal_raises=True)
    )

    class _Forced:
        @staticmethod
        def from_pretrained(name, dtype=None):
            return f"forced:{name}"

    assert _load_lm("x/y", dtype=None, model_class=_Forced) == "forced:x/y"


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
