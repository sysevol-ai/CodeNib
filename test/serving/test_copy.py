# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the copy (CopySpec / prompt-lookup) drafter."""

from codenib.serving.drafter.copy import CopyDrafter


def _linear(tree):
    """Flatten a single-branch draft tree to a token list (root excluded)."""
    tokens = []
    node = tree.root
    while node.children:
        assert len(node.children) == 1, "copy drafter should emit one branch"
        node = node.children[0]
        tokens.append(node.token)
    return tokens


def test_copies_continuation_after_repeated_prefix():
    # "a b c d" appeared once; context now ends in "a b c" -> predict "d ...".
    context = [1, 2, 3, 4, 9, 9, 1, 2, 3]
    drafter = CopyDrafter(min_match=2, max_match=8, max_draft=4)
    tree = drafter.draft(context, max_tokens=4)
    assert _linear(tree) == [4, 9, 9, 1]


def test_prefers_longer_match():
    # Context ends in [8, 2, 3]. The length-2 suffix [2, 3] most-recently
    # continues with 99, but the length-3 suffix [8, 2, 3] continues with 42.
    # Longest-match-first must pick 42, not the nearer-but-shorter 99.
    context = [8, 2, 3, 42, 2, 3, 99, 8, 2, 3]
    drafter = CopyDrafter(min_match=2, max_match=8, max_draft=3)
    tree = drafter.draft(context, max_tokens=3)
    assert _linear(tree)[0] == 42


def test_no_match_returns_empty_tree():
    drafter = CopyDrafter(min_match=3)
    tree = drafter.draft([1, 2, 3, 4, 5], max_tokens=4)
    assert tree.is_empty()


def test_respects_draft_budget():
    context = [1, 2, 3, 4, 5, 6, 1, 2]
    drafter = CopyDrafter(min_match=2, max_match=8, max_draft=10)
    tree = drafter.draft(context, max_tokens=2)
    assert tree.size <= 2


def test_short_context_is_safe():
    drafter = CopyDrafter(min_match=2)
    assert drafter.draft([], 4).is_empty()
    assert drafter.draft([7], 4).is_empty()
