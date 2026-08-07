# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the suffix-automaton drafter."""

from typing import List

from codenib.serving.drafter.copy import CopyDrafter
from codenib.serving.drafter.suffix_automaton import (
    SuffixAutomatonDrafter,
    _build,
    _read_continuation,
)
from codenib.serving.server.worker import SpeculativeConfig, build_draft


def _branches(tree) -> List[List[int]]:
    """Flatten a (linear) draft tree into its root-to-leaf token paths."""
    paths = []

    def walk(node, acc):
        acc = acc + [node.token] if node.token != -1 else acc
        if not node.children:
            if acc:
                paths.append(acc)
            return
        for c in node.children:
            walk(c, acc)

    walk(tree.root, [])
    return paths


# --- automaton primitive -------------------------------------------------


def test_automaton_finds_longest_suffix_match():
    ref = [1, 2, 3, 4, 1, 2, 3]
    sam = _build(ref)
    # Longest suffix of the query that is a substring of ref is [1, 2, 3].
    state, length = sam.longest_suffix_match([9, 9, 1, 2, 3])
    assert length == 3
    # Most-recent occurrence of [1,2,3] ends at index 6 -> read from 7 (empty).
    assert sam.end[state] == 6


def test_automaton_no_match_returns_zero():
    sam = _build([1, 2, 3])
    _, length = sam.longest_suffix_match([7, 8, 9])
    assert length == 0


def test_read_continuation_stops_at_doc_boundary():
    ref = [1, 2, -1, 3, 4]
    assert _read_continuation(ref, 0, 8) == [2]  # stops at the -1 sentinel


# --- in-context (copy-style) drafting ------------------------------------


def test_context_only_predicts_repeat():
    drafter = SuffixAutomatonDrafter(min_match=2, max_draft=8)
    # Suffix [1,2,3] recurs; the earlier occurrence was followed by 4.
    context = [1, 2, 3, 4, 5, 1, 2, 3]
    paths = _branches(drafter.draft(context, max_tokens=8))
    assert paths and paths[0][0] == 4


def test_matches_or_beats_copy_drafter_length():
    # Suffix-automaton finds the exact longest match; copy must not beat it.
    context = [7, 1, 2, 3, 4, 8, 1, 2, 3, 4]
    sam = SuffixAutomatonDrafter(min_match=2, max_draft=16)
    cpy = CopyDrafter(min_match=2, max_match=32, max_draft=16)
    sam_pred = _branches(sam.draft(context, 16))
    cpy_pred = _branches(cpy.draft(context, 16))
    # Both should predict the continuation after "1,2,3,4" -> nothing past it
    # here, so use a case with a real continuation:
    context2 = [1, 2, 3, 9, 0, 1, 2, 3]
    sam_pred = _branches(sam.draft(context2, 16))
    cpy_pred = _branches(cpy.draft(context2, 16))
    assert sam_pred and cpy_pred
    assert len(sam_pred[0]) >= len(cpy_pred[0])
    assert sam_pred[0][0] == cpy_pred[0][0] == 9


def test_no_draft_without_repeat():
    drafter = SuffixAutomatonDrafter(min_match=2, max_draft=8)
    assert drafter.draft([1, 2, 3, 4, 5], max_tokens=8).is_empty()


# --- static corpus (retrieval reach) -------------------------------------


def test_corpus_gives_reach_beyond_context():
    # The continuation lives in another "file", not in the context at all.
    corpus = [[100, 101, 102, 103, 104], [200, 201, 202]]
    drafter = SuffixAutomatonDrafter(corpus=corpus, min_match=2, max_draft=8)
    paths = _branches(drafter.draft([100, 101, 102], max_tokens=8))
    assert paths and paths[0][:2] == [103, 104]


def test_corpus_continuation_respects_doc_boundary():
    corpus = [[1, 2, 3], [4, 5, 6]]  # match in doc 0 must not bleed into doc 1
    drafter = SuffixAutomatonDrafter(corpus=corpus, min_match=2, max_draft=8)
    # Suffix [2,3] ends doc 0; there is no continuation -> no cross-doc draft.
    assert drafter.draft([2, 3], max_tokens=8).is_empty()


# --- drops into fusion ---------------------------------------------------


def test_drops_into_fusion_with_other_drafters():
    context = [1, 2, 3, 4, 1, 2, 3]
    corpus = [[5, 6, 7, 8]]
    sam = SuffixAutomatonDrafter(corpus=corpus, min_match=2, max_draft=8)
    copy = CopyDrafter(min_match=2, max_match=8, max_draft=8)
    config = SpeculativeConfig(max_draft_tokens=8)
    tree = build_draft([copy, sam], context, config)
    assert not tree.is_empty()
    assert tree.size <= 8
