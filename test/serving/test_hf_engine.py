# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for HFTreeEngine — the HF-transformers tree-attention TargetEngine.

The correctness property that matters is that the tree-attention mask isolates
each branch: the model's greedy prediction after any draft node, computed in the
single batched tree forward, must equal the prediction it would make if that
node's root-to-node branch were fed as an ordinary flat sequence. A mock cannot
validate that — it needs a real forward — so these build a tiny random-weights
causal LM on CPU and compare the two. They need torch + transformers (the
``bench`` extra), so they skip when those are unavailable.
"""

from typing import List, Tuple

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")

# Needs the model runtime from the ``serving`` extra, so it is not part of the
# default unit tier.
pytestmark = pytest.mark.slow

from codenib.serving.drafter.copy import CopyDrafter  # noqa: E402
from codenib.serving.server.hf_engine import CachedHFTreeEngine  # noqa: E402
from codenib.serving.server.hf_engine import HFTreeEngine
from codenib.serving.server.sglang import SGLangVerifier  # noqa: E402
from codenib.serving.server.sglang import attention_mask, flatten
from codenib.serving.server.worker import SpeculativeConfig  # noqa: E402
from codenib.serving.server.worker import SpeculativeServer
from codenib.serving.types import DraftNode, DraftTree  # noqa: E402


def _tiny_model():
    """A tiny random-weights Llama causal LM on CPU, eager attention.

    Built from config (no download). Eager attention is required so a custom 4D
    attention mask is honored rather than ignored by a fused kernel.
    """
    from transformers import LlamaConfig, LlamaForCausalLM

    torch.manual_seed(0)
    cfg = LlamaConfig(
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=128,
        attn_implementation="eager",
    )
    return LlamaForCausalLM(cfg).eval()


def _branches(tree: DraftTree) -> List[Tuple[DraftNode, List[int]]]:
    """Every non-root node paired with its root-to-node token list."""
    out: List[Tuple[DraftNode, List[int]]] = []

    def walk(node: DraftNode, prefix: List[int]) -> None:
        for ch in node.children:
            path = prefix + [ch.token]
            out.append((ch, path))
            walk(ch, path)

    walk(tree.root, [])
    return out


def _flat_argmax_after(model, seq: List[int]) -> int:
    """Greedy next-token id the model predicts after the flat sequence ``seq``."""
    with torch.no_grad():
        logits = model(torch.tensor([seq], dtype=torch.long)).logits[0]
    return int(logits[len(seq) - 1].argmax())


def test_predict_returns_one_prediction_per_fed_position():
    model = _tiny_model()
    engine = HFTreeEngine(model, device="cpu")
    context = [3, 1, 4, 1, 5]
    tree = DraftTree()
    tree.add_sequence([9, 2, 6])
    flat = flatten(len(context), tree)
    preds = engine.predict(context, flat)
    assert len(preds) == flat.total_len


def test_tree_verify_matches_per_branch_flat_forward():
    # The core property: each branch's prediction in the batched tree forward
    # equals the prediction from feeding that branch alone as a flat sequence.
    model = _tiny_model()
    engine = HFTreeEngine(model, device="cpu")
    context = [3, 1, 4, 1, 5, 9, 2]
    tree = DraftTree()
    tree.add_sequence([9, 2, 6])  # branch A
    tree.add_sequence([9, 5, 3])  # branch B — shares prefix token 9 with A
    tree.add_sequence([1, 8])  # branch C — separate depth-1 alternative
    flat = flatten(len(context), tree)

    preds = engine.predict(context, flat)

    # Bonus slot: prediction after the last context token must match a plain
    # forward over the context alone.
    assert preds[len(context) - 1] == _flat_argmax_after(model, context)

    # Every draft node: tree-forward prediction == isolated flat-branch prediction.
    for node, branch in _branches(tree):
        gidx = flat.node_index[id(node)]
        assert preds[gidx] == _flat_argmax_after(model, context + branch)


def test_drives_sglang_verifier_end_to_end():
    # HFTreeEngine must drop into SGLangVerifier unchanged and produce a coherent
    # accept/bonus result (accepted prefix consistent with the emitted bonus).
    model = _tiny_model()
    engine = HFTreeEngine(model, device="cpu")
    context = [7, 7, 1, 2, 3]
    tree = DraftTree()
    tree.add_sequence([4, 5, 6])
    verifier = SGLangVerifier(engine)
    result = verifier.verify(context, tree)
    assert isinstance(result.accepted, list)
    assert result.bonus is not None
    # Bonus equals the model's greedy token after the accepted prefix.
    assert result.bonus == _flat_argmax_after(model, context + result.accepted)


# --- vectorized tree mask (follow-up #1): must match the reference helper ------


def test_tree_allow_matches_reference_attention_mask():
    # HFTreeEngine builds its 4D mask with vectorized torch ops instead of the
    # O(total_len^2) Python attention_mask() helper. It must be byte-identical to
    # that trusted reference for every tree shape.
    engine = HFTreeEngine(_tiny_model(), device="cpu")

    linear = DraftTree()
    linear.add_sequence([10, 11, 12])

    branching = DraftTree()
    branching.add_sequence([7, 8])
    branching.add_sequence([7, 9])  # shared prefix 7
    branching.add_sequence([3])  # separate depth-1 branch

    empty = DraftTree()  # no draft -> pure causal context

    for context_len, tree in [(5, linear), (4, branching), (6, empty)]:
        flat = flatten(context_len, tree)
        allow = engine._tree_allow(flat)
        assert allow.tolist() == attention_mask(flat)


# --- CachedHFTreeEngine: KV-cache reuse, must be behaviourally identical -------


def _spec_tokens(engine, prompt, max_new=40):
    server = SpeculativeServer(
        drafters=[CopyDrafter(min_match=2, max_match=8, max_draft=6)],
        config=SpeculativeConfig(max_draft_tokens=6),
    )
    return server.run(prompt, SGLangVerifier(engine), max_new_tokens=max_new).tokens


def test_cached_predict_equals_stateless_at_read_positions():
    # Simulate a growing run: at each step both engines predict on the same
    # (context, tree); the positions the verifier reads (last context + every
    # node) must be identical. Cached forwards only the delta against its KV.
    model = _tiny_model()
    stateless = HFTreeEngine(model, device="cpu")
    cached = CachedHFTreeEngine(model, device="cpu")

    context = [3, 1, 4, 1, 5, 9, 2, 6]
    for grow in ([9, 2], [1, 1, 2], [3], [4, 5]):
        tree = DraftTree()
        tree.add_sequence([7, 8])
        tree.add_sequence([7, 3])  # shares prefix 7 with the first branch
        tree.add_sequence([2])
        flat = flatten(len(context), tree)

        s = stateless.predict(context, flat)
        c = cached.predict(context, flat)

        assert c[len(context) - 1] == s[len(context) - 1]  # bonus slot
        for node, _ in _branches(tree):
            gi = flat.node_index[id(node)]
            assert c[gi] == s[gi]

        context = context + grow  # commit (any monotonic extension is valid)


def test_cached_engine_emits_identical_sequence_end_to_end():
    model = _tiny_model()
    prompt = [5, 5, 1, 2, 3, 1, 2, 3]
    cached = _spec_tokens(CachedHFTreeEngine(model, device="cpu"), prompt)
    stateless = _spec_tokens(HFTreeEngine(model, device="cpu"), prompt)
    assert cached == stateless


def test_cached_engine_resets_between_runs():
    # Reusing one cached engine across different prompts must not leak KV state:
    # each run still matches the stateless reference.
    model = _tiny_model()
    engine = CachedHFTreeEngine(model, device="cpu")
    for prompt in ([1, 2, 3, 4, 1, 2, 3, 4], [9, 8, 7, 6, 9, 8, 7, 6]):
        got = _spec_tokens(engine, prompt)
        want = _spec_tokens(HFTreeEngine(model, device="cpu"), prompt)
        assert got == want


def test_cached_predict_rejects_nonadvancing_empty_draft():
    # Defensive guard: once a context is committed to the cache, calling predict
    # again with the same context and an empty draft leaves nothing new to
    # forward — the bonus-slot logits live only in the cache and are not
    # recomputed, so it must raise rather than run a zero-length forward. The
    # speculation loop never does this (context always advances >= 1 token).
    model = _tiny_model()
    cached = CachedHFTreeEngine(model, device="cpu")
    context = [3, 1, 4, 1, 5]
    tree = DraftTree()
    tree.add_sequence([9, 2])
    cached.predict(context, flatten(len(context), tree))  # commit the prefix
    with pytest.raises(ValueError):
        cached.predict(context, flatten(len(context), DraftTree()))


def test_cached_engine_forwards_fewer_positions():
    # The whole point: cache reuse means far fewer token-positions are pushed
    # through the model than re-forwarding the full context every step.
    model = _tiny_model()
    prompt = [5, 5, 1, 2, 3, 1, 2, 3]
    cached = CachedHFTreeEngine(model, device="cpu")
    stateless = HFTreeEngine(model, device="cpu")
    _spec_tokens(cached, prompt)
    _spec_tokens(stateless, prompt)
    assert cached.forwarded_positions < stateless.forwarded_positions
