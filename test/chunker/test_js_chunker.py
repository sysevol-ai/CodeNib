#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the JavaScript/TypeScript code chunker.
"""

from pathlib import Path

import pytest

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig

pytestmark = pytest.mark.integration


def _collect_chunks(repo_root: Path, chunk_depth: int):
    repo_config = RepoChunkingConfig(languages=["javascript"], filter_tests=False)
    chunker = CodeChunker(
        language="javascript",
        repo_config=repo_config,
        max_lines_per_chunk=None,
        chunk_depth=chunk_depth,
    )
    chunks = chunker.chunk_repository(str(repo_root))
    js_files = sorted(repo_root.rglob("*.js"))
    return chunks, js_files


def test_js_chunker_level_zero(express_repo):
    chunks, js_files = _collect_chunks(express_repo, chunk_depth=0)
    assert js_files, "No JS files detected in express repository"
    assert len(chunks) >= len(js_files)
    assert all(chunk.chunk_type == "file" for chunk in chunks)


def test_js_chunker_level_one(express_repo):
    chunks, _ = _collect_chunks(express_repo, chunk_depth=1)
    chunk_types = {chunk.chunk_type for chunk in chunks}
    assert "method" not in chunk_types
    assert "function" in chunk_types
    # Top-level plain variable/constant declarations should be emitted at L1.
    assert (
        "variable" in chunk_types or "object" in chunk_types
    ), f"Expected variable or object in L1 chunk types, got {chunk_types}"


def test_js_chunker_level_two(express_repo):
    chunks, _ = _collect_chunks(express_repo, chunk_depth=2)
    function_chunks = [chunk for chunk in chunks if chunk.chunk_type == "function"]
    assert function_chunks, "Expected function chunks when chunk_depth=2"
    # Ensure chunker is emitting node identifiers with file context
    assert any(chunk.node_id.startswith("lib/") for chunk in function_chunks)
