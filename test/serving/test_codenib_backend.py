# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for :class:`codenib.serving.drafter.retrieval.CodeNibBackend`.

The CodeNib context and tokenizer are injected, so the whole adapter is
exercised on CPU without installing ``codenib`` or building an index. A
char-level tokenizer makes the suffix-alignment easy to assert exactly.
"""

from __future__ import annotations

import warnings
from typing import List

import pytest

from codenib.serving.drafter.retrieval import CodeNibBackend


class _CharTok:
    """Char-code tokenizer (id == ord(char)); no special tokens."""

    def encode(self, text: str, add_special_tokens: bool = False) -> List[int]:
        return [ord(c) for c in text]

    def decode(self, ids: List[int], skip_special_tokens: bool = True) -> str:
        return "".join(chr(i) for i in ids)


class _Hit:
    """A stand-in for CodeNib's ``NodeInfo`` — only ``.content`` is read."""

    __slots__ = ("content",)

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeBM25:
    def __init__(self, hits: List[_Hit]) -> None:
        self._hits = hits
        self.last_query = None
        self.last_kwargs = None

    def search(self, query, top_k=None, return_code_content=False, **kwargs):
        self.last_query = query
        self.last_kwargs = {
            "top_k": top_k,
            "return_code_content": return_code_content,
            **kwargs,
        }
        return self._hits


class _FakeVector:
    def __init__(self, hits: List[_Hit]) -> None:
        self._hits = hits

    def search_with_content(self, query, top_k=10, **kwargs):
        return self._hits


class _FakeCtx:
    __slots__ = ("bm25", "vector")

    def __init__(self, bm25=None, vector=None) -> None:
        self.bm25 = bm25
        self.vector = vector


_SNIPPET = "def add(a, b): return a + b"


def _backend(ctx, **kw) -> CodeNibBackend:
    return CodeNibBackend(ctx, _CharTok(), key_len=4, top_k=5, **kw)


def test_aligns_retrieved_snippet_to_context_suffix() -> None:
    ctx = _FakeCtx(bm25=_FakeBM25([_Hit(_SNIPPET)]))
    backend = _backend(ctx)
    context = _CharTok().encode("def add(a, b): return ")
    cands = backend.retrieve(context, k=3, max_tokens=8)
    assert cands, "expected a continuation drafted from the retrieved snippet"
    # The suffix "urn " recurs in the snippet; what follows is "a + b".
    assert _CharTok().decode(cands[0]) == "a + b"


def test_query_is_the_decoded_context_suffix() -> None:
    bm25 = _FakeBM25([_Hit(_SNIPPET)])
    backend = _backend(_FakeCtx(bm25=bm25), query_suffix_tokens=5)
    context = _CharTok().encode("xxxxxreturn ")
    backend.retrieve(context, k=1, max_tokens=4)
    # Only the last 5 tokens are decoded into the query.
    assert bm25.last_query == "turn "


def test_context_shorter_than_key_len_returns_empty() -> None:
    backend = _backend(_FakeCtx(bm25=_FakeBM25([_Hit(_SNIPPET)])))
    assert backend.retrieve([1, 2], k=3, max_tokens=8) == []


def test_no_hits_returns_empty() -> None:
    backend = _backend(_FakeCtx(bm25=_FakeBM25([])))
    context = _CharTok().encode("def add(a, b): return ")
    assert backend.retrieve(context, k=3, max_tokens=8) == []


def test_missing_index_returns_empty() -> None:
    backend = _backend(_FakeCtx(bm25=None))  # index built but unavailable
    context = _CharTok().encode("def add(a, b): return ")
    assert backend.retrieve(context, k=3, max_tokens=8) == []


def test_vector_index_path() -> None:
    ctx = _FakeCtx(vector=_FakeVector([_Hit(_SNIPPET)]))
    backend = _backend(ctx, index="vector")
    context = _CharTok().encode("def add(a, b): return ")
    cands = backend.retrieve(context, k=3, max_tokens=8)
    assert cands and _CharTok().decode(cands[0]) == "a + b"


def test_unknown_index_raises() -> None:
    backend = _backend(_FakeCtx(), index="symbol_graph")
    context = _CharTok().encode("def add(a, b): return ")
    with pytest.raises(ValueError, match="unknown CodeNib index"):
        backend.retrieve(context, k=3, max_tokens=8)


def test_bm25_search_requests_clean_snippets() -> None:
    # wrap_with_ln=False keeps line-number prefixes out of the drafted tokens.
    bm25 = _FakeBM25([_Hit(_SNIPPET)])
    backend = _backend(_FakeCtx(bm25=bm25))
    backend.retrieve(_CharTok().encode("def add(a, b): return "), k=1, max_tokens=4)
    assert bm25.last_kwargs["return_code_content"] is True
    assert bm25.last_kwargs["wrap_with_ln"] is False


def test_hits_without_content_warn_instead_of_silently_drafting_nothing() -> None:
    backend = _backend(_FakeCtx(bm25=_FakeBM25([_Hit("")])))
    context = _CharTok().encode("def add(a, b): return ")
    with pytest.warns(RuntimeWarning, match="no snippet content"):
        assert backend.retrieve(context, k=3, max_tokens=8) == []


def test_empty_content_warning_is_emitted_once() -> None:
    backend = _backend(_FakeCtx(bm25=_FakeBM25([_Hit("")])))
    context = _CharTok().encode("def add(a, b): return ")
    with pytest.warns(RuntimeWarning):
        backend.retrieve(context, k=3, max_tokens=8)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a second warning would fail here
        assert backend.retrieve(context, k=3, max_tokens=8) == []


def test_codenib_backend_is_exported_from_the_drafter_package() -> None:
    """``codenib.serving.drafter`` re-exports the other backends; this one belongs too."""
    import codenib.serving.drafter as pkg

    assert pkg.CodeNibBackend is CodeNibBackend
    assert "CodeNibBackend" in pkg.__all__
