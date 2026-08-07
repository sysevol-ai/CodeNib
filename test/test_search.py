# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codenib import search as search_module
from codenib.code_chunking.base import CodeChunk
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.search import CodeSearchEngine
from codenib.types import NODE_TYPE_FILE, NODE_TYPE_FUNCTION, NodeInfo


def _engine(repo_path) -> CodeSearchEngine:
    engine = object.__new__(CodeSearchEngine)
    engine.repo_path = str(repo_path)
    return engine


def test_context_accepts_bm25_node_info_and_includes_end_line(tmp_path):
    (tmp_path / "module.py").write_text("zero\none\ntwo\n", encoding="utf-8")
    engine = _engine(tmp_path)
    result = NodeInfo(
        type=NODE_TYPE_FUNCTION,
        file="module.py",
        start_line=0,
        end_line=1,
    )

    assert engine.get_code_context(result) == "zero\none\n"


def test_file_context_returns_a_bounded_repository_file(tmp_path):
    (tmp_path / "README.md").write_text("repository\n", encoding="utf-8")
    engine = _engine(tmp_path)

    assert (
        engine.get_code_context({"type": NODE_TYPE_FILE, "file": "README.md"})
        == "repository\n"
    )


@pytest.mark.parametrize("path", ["/etc/hosts", "../outside.txt", "a/../../outside"])
def test_context_rejects_paths_outside_the_repository(tmp_path, path):
    engine = _engine(tmp_path)

    assert engine.get_code_context({"type": NODE_TYPE_FILE, "file": path}) is None


def test_context_rejects_an_escaping_symlink(tmp_path):
    outside = tmp_path.parent / "outside-search-context.txt"
    outside.write_text("private\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    engine = _engine(tmp_path)

    assert (
        engine.get_code_context({"type": NODE_TYPE_FILE, "file": "linked.txt"}) is None
    )


def test_context_rejects_oversized_source(monkeypatch, tmp_path):
    monkeypatch.setattr(search_module, "_MAX_SOURCE_BYTES", 4)
    (tmp_path / "large.txt").write_text("12345", encoding="utf-8")
    engine = _engine(tmp_path)

    assert (
        engine.get_code_context({"type": NODE_TYPE_FILE, "file": "large.txt"}) is None
    )


def test_rich_context_normalizes_node_info_and_uses_inclusive_ranges(tmp_path):
    (tmp_path / "module.py").write_text(
        "zero\none\ntwo\nthree\nfour\n",
        encoding="utf-8",
    )
    engine = _engine(tmp_path)
    result = NodeInfo(
        node_name="target()",
        type=NODE_TYPE_FUNCTION,
        file="module.py",
        start_line=2,
        end_line=2,
    )

    enhanced = engine.get_rich_result_context(result, context_lines=1)

    assert isinstance(enhanced, dict)
    assert enhanced["context_code"] == "one\ntwo\nthree\n"
    assert enhanced["context_range"] == {"start": 1, "end": 3}


@pytest.mark.parametrize("context_lines", [-1, True, 1001])
def test_rich_context_rejects_invalid_line_budget(tmp_path, context_lines):
    engine = _engine(tmp_path)

    with pytest.raises(ValueError, match="context_lines"):
        engine.get_rich_result_context({}, context_lines=context_lines)


def test_search_with_context_enriches_real_search_result_shape(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("zero\none needle\ntwo\n", encoding="utf-8")
    engine = _engine(tmp_path)
    engine.bm25_indexer = BM25CodeIndexer(
        chunks=[
            CodeChunk(
                content="one needle",
                start_line=1,
                end_line=1,
                chunk_type=NODE_TYPE_FUNCTION,
                name="target",
                file=str(source),
                node_id="module.py:target()",
            )
        ],
        project_root=str(tmp_path),
    )

    [result] = engine.search_with_context(
        "needle",
        context_lines=0,
        use_keyword_extraction=False,
    )

    assert isinstance(result, dict)
    assert result["context_code"] == "one needle\n"
    assert result["file"] == "module.py"


def test_node_name_context_uses_the_same_path_boundary(tmp_path):
    engine = _engine(tmp_path)
    engine.code_graph = SimpleNamespace(
        get_node_info_by_name=lambda _name: {
            "type": NODE_TYPE_FILE,
            "name": "/etc/hosts",
        }
    )

    assert engine.get_code_context_by_node_name("outside") is None
