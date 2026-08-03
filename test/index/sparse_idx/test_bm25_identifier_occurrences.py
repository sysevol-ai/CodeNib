# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for exact identifier occurrences over BM25 chunk metadata."""

from pathlib import Path

from codenib.code_chunking.base import CodeChunk
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer


def _chunk(
    file: str,
    name: str,
    start_line: int,
    end_line: int,
    content: str,
) -> CodeChunk:
    return CodeChunk(
        content=content,
        start_line=start_line,
        end_line=end_line,
        chunk_type="function",
        name=name,
        file=file,
        node_id=f"{file}:{name}()",
    )


def test_identifier_occurrences_return_callers_not_definition(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def index_is_current(name):\n"
        "    return name == 'fresh'\n"
        "\n"
        "def load_vector(manifest):\n"
        "    if not manifest.index_is_current('vector'):\n"
        "        return None\n"
        "    return open_vector()\n"
        "\n"
        "def unrelated():\n"
        "    index_is_currently = False\n"
        "    return index_is_currently\n",
        encoding="utf-8",
    )
    indexer = BM25CodeIndexer(
        chunks=[
            _chunk(
                "pkg/runtime.py",
                "index_is_current",
                0,
                1,
                "def index_is_current(name): ...",
            ),
            _chunk(
                "pkg/runtime.py",
                "load_vector",
                3,
                6,
                "def load_vector(manifest): ...",
            ),
            _chunk(
                "pkg/runtime.py",
                "unrelated",
                8,
                10,
                "def unrelated(): ...",
            ),
        ],
        project_root=str(repo),
    )

    results = indexer.search_identifier_occurrences("index_is_current")

    assert [node.node_name for node in results] == ["pkg/runtime.py:load_vector()"]
    assert results[0].start_line == 3
    assert "5 |     if not manifest.index_is_current('vector'):" in (
        results[0].content or ""
    )


def test_identifier_occurrences_reject_invalid_identifier(tmp_path: Path) -> None:
    indexer = BM25CodeIndexer(chunks=[], project_root=str(tmp_path))

    try:
        indexer.search_identifier_occurrences("index-is-current")
    except ValueError as exc:
        assert "single code identifier" in str(exc)
    else:
        raise AssertionError("expected an invalid identifier to be rejected")


def test_identifier_occurrences_rank_by_query_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "pkg" / "loaders.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def load_graph(manifest):\n"
        "    return manifest.index_is_current('graph')\n"
        "\n"
        "def load_vector(manifest):\n"
        "    return manifest.index_is_current('vector')\n",
        encoding="utf-8",
    )
    indexer = BM25CodeIndexer(
        chunks=[
            _chunk("pkg/loaders.py", "load_graph", 0, 1, "load graph"),
            _chunk("pkg/loaders.py", "load_vector", 3, 4, "load vector"),
        ],
        project_root=str(repo),
    )

    results = indexer.search_identifier_occurrences(
        "index_is_current",
        context_query="index_is_current vector runtime load",
    )

    assert [node.node_name for node in results] == [
        "pkg/loaders.py:load_vector()",
        "pkg/loaders.py:load_graph()",
    ]
