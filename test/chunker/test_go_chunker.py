#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the Go code chunker.
"""

from pathlib import Path

import pytest

from codenib.code_chunker import CodeChunker, RepoChunkingConfig

pytestmark = pytest.mark.integration


def _collect_chunks(repo_root: Path, chunk_depth: int, l2_level_exclusive: bool = True):
    repo_config = RepoChunkingConfig(languages=["go"])
    chunker = CodeChunker(
        language="go",
        repo_config=repo_config,
        max_lines_per_chunk=None,
        chunk_depth=chunk_depth,
        l2_level_exclusive=l2_level_exclusive,
    )
    chunks = chunker.chunk_repository(str(repo_root))
    go_files = sorted(repo_root.rglob("*.go"))
    return chunks, go_files


def test_go_chunker_level_zero(gjson_repo):
    chunks, go_files = _collect_chunks(gjson_repo, chunk_depth=0)
    assert go_files, "No Go files detected in gjson repository"
    assert len(chunks) >= len(go_files)
    assert all(chunk.chunk_type == "file" for chunk in chunks)


def test_go_chunker_level_one(gjson_repo):
    chunks, _ = _collect_chunks(gjson_repo, chunk_depth=1)
    chunk_types = {chunk.chunk_type for chunk in chunks}
    assert "method" not in chunk_types
    assert "function" in chunk_types
    assert "struct" in chunk_types or "type" in chunk_types
    # Top-level var/const declarations should be emitted at L1.
    assert "var" in chunk_types or "const" in chunk_types


def test_go_chunker_gjson_symbols(gjson_repo):
    chunks, _ = _collect_chunks(gjson_repo, chunk_depth=2, l2_level_exclusive=False)
    gjson_chunks = [c for c in chunks if c.file.endswith("gjson.go")]
    assert gjson_chunks, "Expected chunks from gjson.go"

    names = {c.name for c in gjson_chunks}
    assert "Get" in names, "Expected top-level function Get in gjson.go"
    assert "Result" in names, "Expected type/struct Result in gjson.go"
    assert any(
        name.startswith("Result.") for name in names
    ), "Expected receiver methods for Result in gjson.go"


def test_go_chunker_level_two(gjson_repo):
    chunks, _ = _collect_chunks(gjson_repo, chunk_depth=2)
    method_chunks = [chunk for chunk in chunks if chunk.chunk_type == "method"]
    assert method_chunks, "Expected method chunks when chunk_depth=2"
    # Receiver-qualified names like "Result.Get" should be present.
    assert any("." in chunk.name for chunk in method_chunks)
