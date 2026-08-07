# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for draft-tree fusion and pruning."""

from codenib.serving.drafter.fusion import fuse
from codenib.serving.types import DraftTree


def _make(*sequences, source="x"):
    tree = DraftTree()
    for seq in sequences:
        tree.add_sequence(list(seq), source=source)
    return tree


def test_shared_prefix_merges_into_single_branch():
    a = _make([1, 2, 3])
    b = _make([1, 2, 9])  # shares prefix [1, 2]
    fused = fuse([a, b])
    # root -> 1 -> 2 -> {3, 9}: five real nodes, not six.
    assert fused.size == 4
    first = fused.root.children
    assert len(first) == 1 and first[0].token == 1
    branch = first[0].children[0]  # token 2
    assert sorted(c.token for c in branch.children) == [3, 9]


def test_disjoint_roots_stay_separate():
    fused = fuse([_make([1, 2]), _make([8, 9])])
    assert sorted(c.token for c in fused.root.children) == [1, 8]
    assert fused.size == 4


def test_prune_is_breadth_first():
    # Two depth-3 branches, budget 3 -> keep both roots + one second-level token.
    fused = fuse([_make([1, 2, 3]), _make([8, 9, 10])], max_tokens=3)
    assert fused.size == 3
    depth1 = sorted(c.token for c in fused.root.children)
    assert depth1 == [1, 8]  # shallowest tokens survive


def test_prune_zero_empties_tree():
    fused = fuse([_make([1, 2, 3])], max_tokens=0)
    assert fused.is_empty()


def test_fuse_empty_input():
    assert fuse([]).is_empty()
