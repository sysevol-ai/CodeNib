# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the SGLang tree verifier's engine-agnostic plumbing.

The real GPU forward (``SGLangTreeEngine``) is stubbed, so these exercise the
parts that run without a GPU: tree flattening (positions + tree-attention parent
map), the dense mask expansion, and the greedy accept-path recovery. A
``_GreedyTruthEngine`` stands in for the target model — it predicts the next
token of a known ``truth`` at each position, i.e. a greedy model that continues
``truth``. Driving ``SGLangVerifier`` with it must reproduce ``OracleVerifier``'s
behaviour, proving the flatten/recover machinery is faithful.
"""

from typing import List, Sequence

import pytest

from codenib.serving.drafter.copy import CopyDrafter
from codenib.serving.server.sglang import (
    SGLangTreeEngine,
    SGLangVerifier,
    TargetEngine,
    attention_mask,
    flatten,
)
from codenib.serving.server.worker import (
    OracleVerifier,
    SpeculativeConfig,
    SpeculativeServer,
)
from codenib.serving.types import DraftTree, TokenId


class _GreedyTruthEngine(TargetEngine):
    """Fake target model: greedily continues a known ``truth`` sequence.

    For every fed position ``i`` with position id ``p`` it predicts
    ``truth[p + 1]`` — exactly what a greedy model conditioned on ``truth[:p+1]``
    would emit. ``context`` must be a prefix of ``truth`` (the loop maintains
    this when started from ``truth[:k]``).
    """

    def __init__(self, truth: Sequence[TokenId], eos: int = -999) -> None:
        self.truth = list(truth)
        self.eos = eos

    def predict(self, context: Sequence[TokenId], flat) -> List[TokenId]:
        preds: List[TokenId] = []
        n = len(self.truth)
        # Context positions are 0..context_len-1; draft positions come from flat.
        positions = list(range(flat.context_len)) + list(flat.positions)
        for p in positions:
            preds.append(self.truth[p + 1] if p + 1 < n else self.eos)
        return preds


def _linear_tree(tokens: List[TokenId]) -> DraftTree:
    tree = DraftTree()
    tree.add_sequence(tokens, source="test")
    return tree


# --- flatten -----------------------------------------------------------------


def test_flatten_linear_positions_and_parents():
    # context length 5, a single linear branch of 3 draft tokens.
    flat = flatten(5, _linear_tree([10, 11, 12]))
    assert flat.tokens == [10, 11, 12]
    # Depth 1,2,3 -> positions 5,6,7 (fill the next three generation slots).
    assert flat.positions == [5, 6, 7]
    # Depth-1 node attends to last context token (4); then each to its parent.
    assert flat.parents == [4, 5, 6]
    assert flat.total_len == 8


def test_flatten_branching_shares_context_parent():
    # Two depth-1 alternatives, each is its own branch.
    tree = DraftTree()
    tree.add_sequence([1, 2], source="a")
    tree.add_sequence([3, 4], source="b")
    flat = flatten(2, tree)
    # Pre-order: 1,2 then 3,4. Both depth-1 nodes (1 and 3) share position 2 and
    # attend to the last context token (index 1).
    assert flat.tokens == [1, 2, 3, 4]
    assert flat.positions == [2, 3, 2, 3]
    assert flat.parents == [1, 2, 1, 4]


def test_shared_prefix_collapses_in_flatten():
    # Two continuations agreeing on the first token must share one branch.
    tree = DraftTree()
    tree.add_sequence([7, 8], source="a")
    tree.add_sequence([7, 9], source="b")
    flat = flatten(3, tree)
    # 7 appears once (shared), with two children 8 and 9 both at position 4.
    assert flat.tokens == [7, 8, 9]
    assert flat.positions == [3, 4, 4]
    assert flat.parents == [2, 3, 3]


# --- attention mask ----------------------------------------------------------


def test_attention_mask_tree_structure():
    # context [c0, c1]; branch 7 -> 8, sibling 9 off the same parent 7.
    tree = DraftTree()
    tree.add_sequence([7, 8], source="a")
    tree.add_sequence([7, 9], source="b")
    flat = flatten(2, tree)  # globals: 7@2, 8@3, 9@4
    m = attention_mask(flat)
    # Context is causal.
    assert m[0] == [True, False, False, False, False]
    assert m[1] == [True, True, False, False, False]
    # 7 (idx 2): all context + self.
    assert m[2] == [True, True, True, False, False]
    # 8 (idx 3): context + ancestor 7 + self, NOT sibling 9.
    assert m[3] == [True, True, True, True, False]
    # 9 (idx 4): context + ancestor 7 + self, NOT sibling 8.
    assert m[4] == [True, True, True, False, True]


# --- SGLangVerifier ----------------------------------------------------------


def test_verify_accepts_full_correct_branch():
    truth = [1, 2, 3, 4, 5, 6]
    context = truth[:3]  # [1,2,3]; next true tokens are 4,5,6
    tree = _linear_tree([4, 5, 99])  # 4,5 correct; 99 wrong
    v = SGLangVerifier(_GreedyTruthEngine(truth))
    result = v.verify(context, tree)
    assert result.accepted == [4, 5]
    assert result.bonus == 6  # model's own next token after the accepted prefix


def test_verify_rejects_at_first_divergence():
    truth = [1, 2, 3, 4]
    tree = _linear_tree([9, 9])  # diverges immediately
    v = SGLangVerifier(_GreedyTruthEngine(truth))
    result = v.verify(truth[:2], tree)
    assert result.accepted == []
    assert result.bonus == 3  # bonus token always emitted -> forward progress


def test_verify_picks_correct_branch_among_siblings():
    truth = [1, 2, 3, 4, 5]
    tree = DraftTree()
    tree.add_sequence([9, 9], source="wrong")
    tree.add_sequence([3, 4], source="right")  # matches truth
    v = SGLangVerifier(_GreedyTruthEngine(truth))
    result = v.verify(truth[:2], tree)
    assert result.accepted == [3, 4]
    assert result.bonus == 5


def test_verify_matches_oracle_on_same_tree():
    truth = [5, 6, 7, 8, 9, 10]
    context = truth[:2]
    tree = _linear_tree([7, 8, 42])
    oracle = OracleVerifier(truth).verify(context, tree)
    sglang = SGLangVerifier(_GreedyTruthEngine(truth)).verify(context, tree)
    assert sglang.accepted == oracle.accepted
    assert sglang.bonus == oracle.bonus


def test_verify_empty_tree_still_emits_bonus():
    truth = [1, 2, 3]
    v = SGLangVerifier(_GreedyTruthEngine(truth))
    result = v.verify(truth[:1], DraftTree())
    assert result.accepted == []
    assert result.bonus == 2


def test_eos_prediction_ends_generation():
    truth = [1, 2, 3]
    v = SGLangVerifier(_GreedyTruthEngine(truth, eos=-999), eos_token_id=-999)
    # Context is the whole truth; next prediction runs off the end -> eos.
    result = v.verify(truth, DraftTree())
    assert result.bonus is None


def test_verify_raises_on_mismatched_prediction_count():
    class _BadEngine(TargetEngine):
        def predict(self, context, flat):
            return [0]  # wrong length

    with pytest.raises(ValueError):
        SGLangVerifier(_BadEngine()).verify([1, 2], _linear_tree([3]))


# --- end-to-end through the speculation loop ---------------------------------


def test_run_reconstructs_truth_with_sglang_verifier():
    # Same guarantee as the OracleVerifier loop test, but driven through the
    # real flatten/recover path of SGLangVerifier with a greedy fake engine.
    truth = [1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4, 5]
    server = SpeculativeServer(
        drafters=[CopyDrafter(min_match=2, max_match=16, max_draft=8)],
        config=SpeculativeConfig(max_draft_tokens=8),
    )
    verifier = SGLangVerifier(_GreedyTruthEngine(truth), eos_token_id=-999)
    result = server.run(truth[:4], verifier, max_new_tokens=64)
    assert truth[:4] + result.tokens == truth
    assert result.speedup > 1.0  # copy speculation actually helped


# --- production engine stub ---------------------------------------------------


def test_sglang_tree_engine_is_stub():
    with pytest.raises(NotImplementedError):
        SGLangTreeEngine().predict([1, 2, 3], flatten(3, _linear_tree([4])))
