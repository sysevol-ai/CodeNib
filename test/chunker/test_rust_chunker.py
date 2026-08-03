#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the Rust code chunker.
"""

from pathlib import Path

import pytest

from codenib.code_chunker import CodeChunker, RepoChunkingConfig

pytestmark = pytest.mark.integration


def _collect_chunks(repo_root: Path, chunk_depth: int):
    repo_config = RepoChunkingConfig(languages=["rust"])
    chunker = CodeChunker(
        language="rust",
        repo_config=repo_config,
        max_lines_per_chunk=None,
        chunk_depth=chunk_depth,
    )
    chunks = chunker.chunk_repository(str(repo_root))
    src_dir = repo_root / "src"
    rs_files = sorted(src_dir.rglob("*.rs")) if src_dir.exists() else []
    return chunks, rs_files


def test_rust_chunker_level_zero(arrayvec_repo):
    chunks, rs_files = _collect_chunks(arrayvec_repo, chunk_depth=0)
    assert rs_files, "No Rust files detected in arrayvec repository"
    assert len(chunks) >= len(rs_files)
    assert all(chunk.chunk_type == "file" for chunk in chunks)


def test_rust_chunker_skips_methods_in_level_one(arrayvec_repo):
    chunks, _ = _collect_chunks(arrayvec_repo, chunk_depth=1)
    chunk_types = {chunk.chunk_type for chunk in chunks}
    assert "method" not in chunk_types
    assert {"function", "struct", "impl"}.issubset(chunk_types)
    # Top-level const/static/type declarations should be present when the
    # repo uses them (arrayvec defines type aliases and constants).
    assert chunk_types & {
        "const",
        "static",
        "type",
    }, f"Expected at least one of const/static/type in L1 chunk types, got {chunk_types}"


def test_rust_chunker_includes_methods_in_level_two(arrayvec_repo):
    chunks, _ = _collect_chunks(arrayvec_repo, chunk_depth=2)
    method_chunks = [chunk for chunk in chunks if chunk.chunk_type == "method"]
    assert method_chunks, "Expected method chunks when chunk_depth=2"
    method_names = [chunk.name for chunk in method_chunks]
    assert any("push" in name for name in method_names)
