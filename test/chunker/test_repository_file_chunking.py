# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig


def _identity(chunk):
    return (
        chunk.content,
        chunk.start_line,
        chunk.end_line,
        chunk.chunk_type,
        chunk.name,
        chunk.file,
        chunk.node_id,
    )


def test_single_file_reuses_repository_dispatch_and_filters(tmp_path):
    source = tmp_path / "source.js"
    source.write_text(
        "export function outer() {\n"
        "  function inner() { return 1; }\n"
        "  return inner();\n"
        "}\n",
        encoding="utf-8",
    )
    test_source = tmp_path / "source.test.js"
    test_source.write_text("export function testOuter() {}\n", encoding="utf-8")
    kwargs = {
        "language": "typescript",
        "repo_config": RepoChunkingConfig(languages=["typescript"]),
        "chunk_depth": 2,
        "l2_level_exclusive": True,
        "skeleton_mode": False,
    }

    repository_chunks = CodeChunker(**kwargs).chunk_repository(str(tmp_path))
    single_chunks = CodeChunker(**kwargs).chunk_repository_file(
        str(source), str(tmp_path)
    )

    assert [_identity(chunk) for chunk in single_chunks] == [
        _identity(chunk) for chunk in repository_chunks if chunk.file == str(source)
    ]
    assert (
        CodeChunker(**kwargs).chunk_repository_file(str(test_source), str(tmp_path))
        == []
    )


def test_single_file_rejects_paths_outside_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def f(): pass\n", encoding="utf-8")
    chunker = CodeChunker(
        language="python",
        repo_config=RepoChunkingConfig(languages=["python"]),
    )

    with pytest.raises(ValueError, match="outside repository"):
        chunker.chunk_repository_file(str(outside), str(repo))
