# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.code_chunking.base import CodeChunk
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer


def _chunk(
    content: str,
    start_line: int,
    end_line: int,
    *,
    node_id: str = "include/fmt/format.h:to_string()",
) -> CodeChunk:
    return CodeChunk(
        content,
        start_line,
        end_line,
        "function",
        "to_string",
        "include/fmt/format.h",
        node_id,
    )


def test_bm25_keeps_overloaded_symbol_spans_independent():
    indexer = BM25CodeIndexer(
        chunks=[
            _chunk("buffer conversion overload_marker", 956, 961),
            _chunk("integral conversion integer_marker", 4429, 4434),
        ],
        max_k=8,
    )

    assert len(indexer.documents) == 2
    assert indexer.nodes == ["include/fmt/format.h:to_string()"]
    assert [
        (document.metadata["start_line"], document.metadata["end_line"])
        for document in indexer.documents
    ] == [(956, 961), (4429, 4434)]

    result = indexer.search(
        "integer_marker",
        top_k=1,
        return_code_content=False,
    )[0]
    assert (result.start_line, result.end_line) == (4429, 4434)


def test_bm25_preserves_bounded_split_documents_across_reload(tmp_path):
    node_id = "src/large.py:run()"
    indexer = BM25CodeIndexer(
        chunks=[
            _chunk("first bounded segment first_marker", 10, 209, node_id=node_id),
            _chunk("second bounded segment second_marker", 210, 409, node_id=node_id),
            _chunk("duplicate extraction", 210, 409, node_id=node_id),
        ],
        max_k=8,
    )

    assert len(indexer.documents) == 2
    assert indexer.nodes == [node_id]

    indexer.save_index(str(tmp_path))
    loaded = BM25CodeIndexer()
    loaded.load_index(str(tmp_path))

    assert len(loaded.documents) == 2
    assert loaded.nodes == [node_id]
    result = loaded.search(
        "second_marker",
        top_k=1,
        return_code_content=False,
    )[0]
    assert (result.start_line, result.end_line) == (210, 409)
