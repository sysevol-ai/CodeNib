# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from codenib.code_chunking.base import CodeChunk
from codenib.index.sparse_idx.bm25_index import (
    _JSON_WRITE_CHARS,
    BM25CodeIndexer,
    BM25Retriever,
    _dump_json_interruptibly,
)


def _chunk(content: str, line: int, name: str) -> CodeChunk:
    return CodeChunk(
        content=content,
        start_line=line,
        end_line=line,
        chunk_type="function",
        name=name,
        file="pkg/search.py",
        node_id=f"pkg/search.py:{name}()",
    )


def test_chunk_index_persists_source_location_and_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "calculator.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def release_signature(left: int, right: int) -> int:\n"
        '    """Return a deterministic value."""\n'
        "    return left + right\n",
        encoding="utf-8",
    )
    node_id = "pkg/calculator.py:release_signature()"
    chunks = [
        CodeChunk(
            content="def release_signature(left: int, right: int) -> int:",
            start_line=0,
            end_line=1,
            chunk_type="function",
            name="release_signature",
            file=str(source),
            node_id=node_id,
        ),
        CodeChunk(
            content="    return left + right",
            start_line=2,
            end_line=2,
            chunk_type="function",
            name="release_signature",
            file=str(source),
            node_id=node_id,
        ),
    ]

    indexer = BM25CodeIndexer(chunks=chunks, project_root=str(repo))
    index_dir = tmp_path / "bm25"
    indexer.save_index(str(index_dir))

    loaded = BM25CodeIndexer()
    loaded.load_index(str(index_dir))
    results = loaded.search(
        "release_signature",
        top_k=2,
        return_code_content=True,
        wrap_with_ln=False,
    )

    assert len(loaded.documents) == 2
    assert loaded.project_root == str(repo)
    assert {result.file for result in results} == {"pkg/calculator.py"}
    assert {(result.start_line, result.end_line) for result in results} == {
        (0, 1),
        (2, 2),
    }
    assert {result.type for result in results} == {"function"}
    assert any("return left + right" in result.content for result in results)


def test_chunk_index_ranks_terms_from_implementation_body(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def opaque_loader(manifest):\n"
        "    if not manifest.index_is_current('vector'):\n"
        "        return None\n"
        "    return load_vector()\n"
        "\n"
        "def unrelated_helper():\n"
        "    return 7\n"
        "\n"
        "def another_helper():\n"
        "    return 8\n",
        encoding="utf-8",
    )
    indexer = BM25CodeIndexer(
        chunks=[
            CodeChunk(
                content=(
                    "def opaque_loader(manifest):\n"
                    "    if not manifest.index_is_current('vector'):\n"
                    "        return None"
                ),
                start_line=0,
                end_line=3,
                chunk_type="function",
                name="opaque_loader",
                file=str(source),
                node_id="pkg/runtime.py:opaque_loader()",
            ),
            CodeChunk(
                content="def unrelated_helper():\n    return 7",
                start_line=5,
                end_line=6,
                chunk_type="function",
                name="unrelated_helper",
                file=str(source),
                node_id="pkg/runtime.py:unrelated_helper()",
            ),
            CodeChunk(
                content="def another_helper():\n    return 8",
                start_line=8,
                end_line=9,
                chunk_type="function",
                name="another_helper",
                file=str(source),
                node_id="pkg/runtime.py:another_helper()",
            ),
        ],
        project_root=str(repo),
    )

    results = indexer.search("stale vector index current", top_k=1)

    assert results[0].node_name == "pkg/runtime.py:opaque_loader()"
    assert {"index", "is", "current", "vector"} <= set(
        indexer.documents[0].page_content.split()
    )


def test_loaded_index_restores_candidate_limit_before_retriever(tmp_path: Path) -> None:
    chunks = [
        CodeChunk(
            content=f"def symbol_{index}(): return {index}",
            start_line=index,
            end_line=index,
            chunk_type="function",
            name=f"symbol_{index}",
            file="pkg/many.py",
            node_id=f"pkg/many.py:symbol_{index}()",
        )
        for index in range(24)
    ]
    indexer = BM25CodeIndexer(chunks=chunks, max_k=32)
    index_dir = tmp_path / "bm25"
    indexer.save_index(str(index_dir))

    loaded = BM25CodeIndexer()
    loaded.load_index(str(index_dir))

    assert loaded.max_k == 32
    assert loaded.retriever is not None
    assert loaded.retriever.k == 32
    assert len(loaded.search("symbol", top_k=20)) == 20


def test_prepare_only_index_persists_exact_eager_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        _chunk("def alpha(): return 'ordinary term'", 0, "alpha"),
        _chunk("def omega(): return 'unique omega marker'", 2, "omega"),
    ]
    eager = BM25CodeIndexer(chunks=chunks, max_k=8, project_root="/repo")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            BM25Retriever,
            "from_documents",
            classmethod(
                lambda _cls, *_args, **_kwargs: pytest.fail(
                    "prepare-only construction built an in-memory rank index"
                )
            ),
        )
        prepared = BM25CodeIndexer(
            chunks=chunks,
            max_k=8,
            project_root="/repo",
            prepare_only=True,
        )

    assert prepared.retriever is None
    eager_root = tmp_path / "eager"
    prepared_root = tmp_path / "prepared"
    eager.save_index(str(eager_root))
    prepared.save_index(str(prepared_root))
    for name in ("documents.json", "bm25_metadata.json"):
        assert (prepared_root / name).read_bytes() == (eager_root / name).read_bytes()

    loaded = BM25CodeIndexer()
    loaded.load_index(str(prepared_root))
    assert [item.node_name for item in loaded.search("omega", top_k=2)] == [
        item.node_name for item in eager.search("omega", top_k=2)
    ]

    empty = BM25CodeIndexer(chunks=[], prepare_only=True)
    empty_root = tmp_path / "empty"
    empty.save_index(str(empty_root))
    assert (empty_root / "documents.json").read_bytes() == b"[]"


def test_prepare_only_index_rejects_graph_precedence_ambiguity() -> None:
    with pytest.raises(ValueError, match="without a code graph"):
        BM25CodeIndexer(
            code_graph=object(),
            chunks=[],
            prepare_only=True,
            check_cancelled=lambda: None,
        )


def test_prepare_only_index_stops_between_chunk_conversions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        _chunk("def first(): return 1", 0, "first"),
        _chunk("def second(): return 2", 2, "second"),
    ]
    stopped = KeyboardInterrupt("stop between BM25 document conversions")
    converted = 0
    armed = False
    real_convert = BM25CodeIndexer._convert_chunk_to_document

    def convert(indexer, chunk):
        nonlocal armed, converted
        document = real_convert(indexer, chunk)
        converted += 1
        armed = True
        return document

    def check_cancelled() -> None:
        if armed:
            raise stopped

    monkeypatch.setattr(BM25CodeIndexer, "_convert_chunk_to_document", convert)
    monkeypatch.setattr(
        BM25Retriever,
        "from_documents",
        classmethod(
            lambda _cls, *_args, **_kwargs: pytest.fail(
                "rank construction ran after conversion cancellation"
            )
        ),
    )

    with pytest.raises(BaseException) as raised:
        BM25CodeIndexer(
            chunks=chunks,
            prepare_only=True,
            check_cancelled=check_cancelled,
        )

    assert raised.value is stopped
    assert converted == 1


def test_interruptible_json_dump_stops_after_first_write() -> None:
    stopped = KeyboardInterrupt("stop during BM25 JSON persistence")
    writes: list[str] = []
    armed = False

    class Writer:
        def write(self, payload: str) -> None:
            nonlocal armed
            writes.append(payload)
            if len(payload) == _JSON_WRITE_CHARS:
                armed = True

    def check_cancelled() -> None:
        if armed:
            raise stopped

    with pytest.raises(BaseException) as raised:
        _dump_json_interruptibly(
            [{"payload": "x" * (2 * 1024 * 1024)}],
            Writer(),
            check_cancelled,
        )

    assert raised.value is stopped
    assert armed
    assert max(map(len, writes)) <= _JSON_WRITE_CHARS
    assert sum(map(len, writes)) < 2 * 1024 * 1024


def test_interruptible_json_save_preserves_stop_over_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.index.sparse_idx.bm25_index as bm25_module

    stopped = KeyboardInterrupt("stop during BM25 JSON persistence")
    close_failure = OSError("injected JSON close failure")
    armed = False

    class Writer:
        def write(self, payload: str) -> int:
            nonlocal armed
            armed = True
            return len(payload)

        def close(self) -> None:
            raise close_failure

    def check_cancelled() -> None:
        if armed:
            raise stopped

    monkeypatch.setattr(
        bm25_module, "open", lambda *_args, **_kwargs: Writer(), raising=False
    )
    indexer = BM25CodeIndexer(chunks=[], prepare_only=True)

    with pytest.raises(BaseException) as raised:
        indexer.save_index(
            str(tmp_path),
            check_cancelled=check_cancelled,
        )

    assert raised.value is stopped
    assert raised.value.__cause__ is close_failure


def test_legacy_chunk_indexer_override_remains_persistable(tmp_path: Path) -> None:
    class LegacyIndexer(BM25CodeIndexer):
        def build_index_from_chunks(self, chunks, *, project_root=None):
            self.documents = []
            self.retriever = object()
            return self.retriever

    indexer = LegacyIndexer(chunks=[])
    indexer.save_index(str(tmp_path))

    assert (tmp_path / "documents.json").read_bytes() == b"[]"


def test_failed_eager_rank_build_is_not_persistable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = RuntimeError("injected BM25 rank construction failure")
    indexer = BM25CodeIndexer()

    def fail(_cls, *_args, **_kwargs):
        raise failed

    monkeypatch.setattr(BM25Retriever, "from_documents", classmethod(fail))

    with pytest.raises(BaseException) as raised:
        indexer.build_index_from_chunks([_chunk("def value(): return 1", 0, "value")])

    assert raised.value is failed
    assert indexer.retriever is None
    with pytest.raises(ValueError, match="Index has not been built"):
        indexer.save_index(str(tmp_path / "failed"))
    assert not (tmp_path / "failed").exists()
