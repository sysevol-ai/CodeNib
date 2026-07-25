#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the C++ code chunker using the fmt repository.
"""

from pathlib import Path

import pytest

from codenib.code_chunker import CodeChunker, RepoChunkingConfig

pytestmark = pytest.mark.integration


def _collect_chunks(repo_root: Path, chunk_depth: int):
    repo_config = RepoChunkingConfig(languages=["cpp"])
    chunker = CodeChunker(
        language="cpp",
        repo_config=repo_config,
        max_lines_per_chunk=None,
        chunk_depth=chunk_depth,
    )
    chunks = chunker.chunk_repository(str(repo_root))

    cpp_files = []
    for dirname in ("include", "src"):
        candidate = repo_root / dirname
        if not candidate.exists():
            continue
        for file_path in candidate.rglob("*"):
            if file_path.suffix in repo_config.cpp_extensions:
                cpp_files.append(file_path)

    cpp_files.sort()
    return chunks, cpp_files


def test_cpp_chunker_level_zero(fmt_repo):
    chunks, cpp_files = _collect_chunks(fmt_repo, chunk_depth=0)
    assert cpp_files, "No C++ files detected in fmt repository"
    assert len(chunks) >= len(cpp_files)
    assert all(chunk.chunk_type == "file" for chunk in chunks)


def test_cpp_chunker_top_level_only(fmt_repo):
    chunks, _ = _collect_chunks(fmt_repo, chunk_depth=1)
    chunk_types = {chunk.chunk_type for chunk in chunks}
    assert "function" in chunk_types
    assert "method" not in chunk_types
    # Top-level declarations or macros should be emitted at L1.
    assert chunk_types & {
        "declaration",
        "macro",
    }, f"Expected declaration or macro in L1 chunk types, got {chunk_types}"


def test_cpp_chunker_includes_methods(fmt_repo):
    chunks, _ = _collect_chunks(fmt_repo, chunk_depth=2)
    method_chunks = [chunk for chunk in chunks if chunk.chunk_type == "method"]
    assert method_chunks, "Expected method chunks when chunk_depth=2"
    assert any("include/fmt/format.h" in str(chunk.file) for chunk in method_chunks)
