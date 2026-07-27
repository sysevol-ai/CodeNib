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
