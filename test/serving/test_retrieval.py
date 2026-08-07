# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the retrieval drafter and the n-gram corpus backend."""

from codenib.serving.drafter.retrieval import NgramRetrievalBackend, RetrievalDrafter


def test_ngram_backend_finds_continuation_in_other_doc():
    # The pattern [1,2,3,4] lives in doc 0; querying with [..,1,2,3,4] should
    # surface its continuation [5,6,7] even though it is not in the context.
    corpus = [[9, 1, 2, 3, 4, 5, 6, 7], [0, 0, 0]]
    backend = NgramRetrievalBackend(corpus, key_len=4)
    out = backend.retrieve([1, 2, 3, 4], k=2, max_tokens=3)
    assert out and out[0] == [5, 6, 7]


def test_ngram_backend_ranks_by_match_length():
    # Two occurrences of [2,3]; the one with a longer left-context match wins.
    corpus = [[7, 1, 2, 3, 40], [8, 9, 1, 2, 3, 99]]
    backend = NgramRetrievalBackend(corpus, key_len=2)
    out = backend.retrieve([9, 1, 2, 3], k=1, max_tokens=1)
    assert out[0] == [99]


def test_ngram_backend_respects_doc_boundaries():
    # Continuation must not bleed past the end of the matched document.
    corpus = [[1, 2, 3], [4, 5, 6]]
    backend = NgramRetrievalBackend(corpus, key_len=2)
    out = backend.retrieve([1, 2], k=1, max_tokens=5)
    assert out[0] == [3]  # stops at doc 0's boundary, no [4,5,6]


def test_ngram_backend_miss_returns_empty():
    backend = NgramRetrievalBackend([[1, 2, 3, 4]], key_len=4)
    assert backend.retrieve([7, 8, 9, 10], k=2, max_tokens=3) == []


def test_retrieval_drafter_builds_tree_from_backend():
    backend = NgramRetrievalBackend([[1, 2, 3, 4, 5, 6]], key_len=3)
    drafter = RetrievalDrafter(backend, k=2, max_draft=4)
    tree = drafter.draft([1, 2, 3], max_tokens=4)
    assert not tree.is_empty()
    # First speculated token after [1,2,3] is 4.
    assert tree.root.children[0].token == 4
