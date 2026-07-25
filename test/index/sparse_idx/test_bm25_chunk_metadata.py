# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from codenib.code_chunking.base import CodeChunk
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer


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
        top_k=1,
        return_code_content=True,
        wrap_with_ln=False,
    )

    assert len(loaded.documents) == 1
    assert loaded.project_root == str(repo)
    assert results[0].file == "pkg/calculator.py"
    assert results[0].start_line == 0
    assert results[0].end_line == 2
    assert results[0].type == "function"
    assert "return left + right" in results[0].content
