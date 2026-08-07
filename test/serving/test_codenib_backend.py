# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for :class:`codenib.serving.drafter.retrieval.CodeNibBackend`.

The CodeNib context and tokenizer are injected, so the whole adapter is
exercised on CPU without installing ``codenib`` or building an index. A
char-level tokenizer makes the suffix-alignment easy to assert exactly.
"""

from __future__ import annotations

import os
import sys
import types
import warnings
from types import SimpleNamespace
from typing import List

import pytest

from codenib.serving.drafter.retrieval import (
    MAX_RETRIEVAL_BATCH_CHARACTERS,
    MAX_RETRIEVAL_QUERIES_PER_RUN,
    MAX_RETRIEVAL_RUN_CHARACTERS,
    MAX_RETRIEVAL_RUN_SOURCE_BYTES,
    MAX_RETRIEVAL_SOURCE_FILE_BYTES,
    CodeNibBackend,
    RetrievalDrafter,
    _open_regular_file_beneath,
)
from codenib.types import NODE_TYPE_FILE


class _CharTok:
    """Char-code tokenizer (id == ord(char)); no special tokens."""

    def encode(self, text: str, add_special_tokens: bool = False) -> List[int]:
        return [ord(c) for c in text]

    def decode(self, ids: List[int], skip_special_tokens: bool = True) -> str:
        return "".join(chr(i) for i in ids)


class _Hit:
    """A small stand-in for CodeNib's persisted ``NodeInfo`` metadata."""

    __slots__ = ("content", "end_line", "file", "start_line", "type")

    def __init__(
        self,
        content: str | None,
        *,
        file: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        type: str = "symbol",
    ) -> None:
        self.content = content
        self.file = file
        self.start_line = start_line
        self.end_line = end_line
        self.type = type


class _FakeBM25:
    def __init__(self, hits: List[_Hit], *, project_root=None) -> None:
        self._hits = hits
        self.project_root = project_root
        self.calls = 0
        self.last_query = None
        self.last_kwargs = None

    def search(self, query, top_k=None, return_code_content=False, **kwargs):
        self.calls += 1
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

    def search(self, query, top_k=10, **kwargs):
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


@pytest.mark.parametrize("index", ["bm25", "vector"])
def test_from_manifest_loads_only_selected_view(monkeypatch, index: str) -> None:
    selected = _FakeBM25([]) if index == "bm25" else _FakeVector([])
    ctx = SimpleNamespace(
        manifest=SimpleNamespace(indexes={index: object()}),
        errors={},
        bm25=selected if index == "bm25" else None,
        vector=selected if index == "vector" else None,
    )
    seen = {}

    class _ServerContext:
        @staticmethod
        def load(path, *, views=None):
            seen["path"] = path
            seen["views"] = views
            return ctx

    module = types.ModuleType("codenib.mcp.context")
    module.ServerContext = _ServerContext
    monkeypatch.setitem(sys.modules, "codenib.mcp.context", module)

    backend = CodeNibBackend.from_manifest("/manifest.json", _CharTok(), index=index)

    assert backend.ctx is ctx
    assert seen == {"path": "/manifest.json", "views": {index}}


def test_from_manifest_rejects_unknown_view_before_loading(monkeypatch) -> None:
    class _ServerContext:
        @staticmethod
        def load(*args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("unexpected manifest load")

    module = types.ModuleType("codenib.mcp.context")
    module.ServerContext = _ServerContext
    monkeypatch.setitem(sys.modules, "codenib.mcp.context", module)

    with pytest.raises(ValueError, match="unknown CodeNib index"):
        CodeNibBackend.from_manifest("/manifest.json", _CharTok(), index="zoekt")


@pytest.mark.parametrize(
    ("entry", "current", "errors", "expected"),
    [
        (None, True, {}, "no entry"),
        (object(), False, {}, "stale"),
        (object(), True, {"bm25": "broken artifact"}, "broken artifact"),
    ],
)
def test_from_manifest_fails_when_selected_view_is_unavailable(
    monkeypatch,
    entry,
    current: bool,
    errors: dict,
    expected: str,
) -> None:
    manifest = SimpleNamespace(
        indexes={} if entry is None else {"bm25": entry},
        index_is_current=lambda _index: current,
    )
    ctx = SimpleNamespace(manifest=manifest, errors=errors, bm25=None, vector=None)

    class _ServerContext:
        @staticmethod
        def load(path, *, views=None):
            return ctx

    module = types.ModuleType("codenib.mcp.context")
    module.ServerContext = _ServerContext
    monkeypatch.setitem(sys.modules, "codenib.mcp.context", module)

    with pytest.raises(RuntimeError, match=expected):
        CodeNibBackend.from_manifest("/manifest.json", _CharTok(), index="bm25")


def test_bm25_search_defers_source_content_to_bounded_reader() -> None:
    bm25 = _FakeBM25([_Hit(_SNIPPET)])
    backend = _backend(_FakeCtx(bm25=bm25))
    backend.retrieve(_CharTok().encode("def add(a, b): return "), k=1, max_tokens=4)
    assert bm25.last_kwargs["return_code_content"] is False


class _RecordingTok(_CharTok):
    def __init__(self) -> None:
        self.encoded: List[str] = []

    def encode(self, text: str, add_special_tokens: bool = False) -> List[int]:
        self.encoded.append(text)
        return super().encode(text, add_special_tokens=add_special_tokens)


def _source_backend(root, hits, tokenizer=None) -> CodeNibBackend:
    return CodeNibBackend(
        _FakeCtx(bm25=_FakeBM25(hits, project_root=str(root))),
        tokenizer or _CharTok(),
        key_len=4,
        top_k=len(hits),
    )


def test_oversized_single_line_file_never_reaches_tokenizer(tmp_path) -> None:
    source = tmp_path / "huge.py"
    with source.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024)
    tokenizer = _RecordingTok()
    backend = _source_backend(
        tmp_path,
        [_Hit(None, file="huge.py", type=NODE_TYPE_FILE)],
        tokenizer,
    )

    with pytest.warns(RuntimeWarning, match="no usable bounded"):
        assert backend.retrieve(_CharTok().encode("abcd"), k=1, max_tokens=4) == []
    assert tokenizer.encoded == []


def test_source_symlink_escape_never_reaches_tokenizer(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("abcd secret", encoding="utf-8")
    (repo / "escape.py").symlink_to(outside)
    tokenizer = _RecordingTok()
    backend = _source_backend(
        repo,
        [_Hit(None, file="escape.py", start_line=0, end_line=0)],
        tokenizer,
    )

    with pytest.warns(RuntimeWarning, match="no usable bounded"):
        assert backend.retrieve(_CharTok().encode("abcd"), k=1, max_tokens=4) == []
    assert tokenizer.encoded == []


def test_final_component_swap_to_symlink_is_rejected(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("safe", encoding="utf-8")
    resolved = source.resolve()
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)

    assert _open_regular_file_beneath(repo.resolve(), resolved) is None


def test_intermediate_component_swap_to_symlink_is_rejected(tmp_path) -> None:
    repo = tmp_path / "repo"
    inside = repo / "pkg"
    inside.mkdir(parents=True)
    source = inside / "source.py"
    source.write_text("safe", encoding="utf-8")
    resolved = source.resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.py").write_text("secret", encoding="utf-8")
    inside.rename(repo / "old-pkg")
    inside.symlink_to(outside, target_is_directory=True)

    assert _open_regular_file_beneath(repo.resolve(), resolved) is None


def test_fifo_source_is_rejected_without_blocking(tmp_path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO test requires os.mkfifo")

    os.mkfifo(tmp_path / "source.py")
    tokenizer = _RecordingTok()
    backend = _source_backend(
        tmp_path,
        [_Hit(None, file="source.py", type=NODE_TYPE_FILE)],
        tokenizer,
    )

    with pytest.warns(RuntimeWarning, match="no usable bounded"):
        assert backend.retrieve(_CharTok().encode("abcd"), k=1, max_tokens=4) == []
    assert tokenizer.encoded == []


def test_symbol_range_can_be_read_late_without_loading_unbounded_content(
    tmp_path,
) -> None:
    source = tmp_path / "late.py"
    source.write_text("ignore\n" * 100 + "abcdTAIL\n", encoding="utf-8")
    tokenizer = _RecordingTok()
    backend = _source_backend(
        tmp_path,
        [_Hit(None, file="late.py", start_line=100, end_line=100)],
        tokenizer,
    )

    candidates = backend.retrieve(_CharTok().encode("abcd"), k=1, max_tokens=4)

    assert tokenizer.encoded == ["abcdTAIL\n"]
    assert candidates and _CharTok().decode(candidates[0]) == "TAIL"


def test_symbol_without_range_does_not_widen_to_whole_file(tmp_path) -> None:
    (tmp_path / "missing.py").write_text("abcd secret", encoding="utf-8")
    tokenizer = _RecordingTok()
    backend = _source_backend(
        tmp_path,
        [_Hit(None, file="missing.py")],
        tokenizer,
    )

    with pytest.warns(RuntimeWarning, match="no usable bounded"):
        assert backend.retrieve(_CharTok().encode("abcd"), k=1, max_tokens=4) == []
    assert tokenizer.encoded == []


def test_batch_character_budget_is_applied_before_tokenization(tmp_path) -> None:
    hits = []
    for index in range(20):
        path = tmp_path / f"hit-{index}.py"
        path.write_text("abcd" + "x" * 4092, encoding="utf-8")
        hits.append(_Hit(None, file=path.name, type=NODE_TYPE_FILE))
    tokenizer = _RecordingTok()
    backend = _source_backend(tmp_path, hits, tokenizer)

    backend.retrieve(_CharTok().encode("abcd"), k=20, max_tokens=4)

    assert len(tokenizer.encoded) < len(hits)
    assert sum(map(len, tokenizer.encoded)) <= MAX_RETRIEVAL_BATCH_CHARACTERS


def test_run_character_budget_is_cumulative_across_queries() -> None:
    tokenizer = _RecordingTok()
    snippet = "abcd" + "x" * 16_000
    bm25 = _FakeBM25([_Hit(snippet)])
    backend = CodeNibBackend(
        _FakeCtx(bm25=bm25),
        tokenizer,
        key_len=4,
        top_k=1,
    )
    backend.begin_run()

    with pytest.warns(RuntimeWarning, match="no usable bounded"):
        for _ in range(MAX_RETRIEVAL_QUERIES_PER_RUN + 1):
            backend.retrieve(_CharTok().encode("abcd"), k=1, max_tokens=4)

    assert sum(map(len, tokenizer.encoded)) <= MAX_RETRIEVAL_RUN_CHARACTERS
    assert len(tokenizer.encoded) < MAX_RETRIEVAL_QUERIES_PER_RUN


def test_run_source_byte_budget_is_cumulative_across_queries(tmp_path) -> None:
    source = tmp_path / "source.py"
    with source.open("wb") as handle:
        handle.write(b"abcdTAIL\n")
        handle.truncate(MAX_RETRIEVAL_SOURCE_FILE_BYTES)
    tokenizer = _RecordingTok()
    bm25 = _FakeBM25(
        [_Hit(None, file=source.name, start_line=0, end_line=0)],
        project_root=str(tmp_path),
    )
    backend = CodeNibBackend(
        _FakeCtx(bm25=bm25),
        tokenizer,
        key_len=4,
        top_k=1,
    )
    backend.begin_run()

    for _ in range(MAX_RETRIEVAL_QUERIES_PER_RUN):
        backend.retrieve(_CharTok().encode("abcd"), k=1, max_tokens=4)

    expected_reads = MAX_RETRIEVAL_RUN_SOURCE_BYTES // MAX_RETRIEVAL_SOURCE_FILE_BYTES
    assert len(tokenizer.encoded) == expected_reads
    assert bm25.calls == expected_reads


def test_retrieval_drafter_caps_queries_and_resets_each_run() -> None:
    class _CountingBackend:
        calls = 0

        def retrieve(self, context, k, max_tokens):
            self.calls += 1
            return []

    backend = _CountingBackend()
    drafter = RetrievalDrafter(backend, max_queries_per_run=2)

    drafter.begin_run()
    for _ in range(3):
        assert drafter.draft([1, 2, 3, 4], max_tokens=4).is_empty()
    assert backend.calls == 2

    drafter.begin_run()
    drafter.draft([1, 2, 3, 4], max_tokens=4)
    assert backend.calls == 3


def test_hits_without_content_warn_instead_of_silently_drafting_nothing() -> None:
    backend = _backend(_FakeCtx(bm25=_FakeBM25([_Hit("")])))
    context = _CharTok().encode("def add(a, b): return ")
    with pytest.warns(RuntimeWarning, match="no usable bounded"):
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
