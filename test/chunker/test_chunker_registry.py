# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for registry-driven chunker factory routing."""

from __future__ import annotations

import pytest

from codeminer.code_chunking import create_chunker


@pytest.mark.parametrize(
    ("language", "class_name"),
    [
        ("python", "PythonCodeChunker"),
        ("go", "GoCodeChunker"),
        ("golang", "GoCodeChunker"),
        ("rust", "RustCodeChunker"),
        ("cpp", "CppCodeChunker"),
        ("c++", "CppCodeChunker"),
        ("java", "JavaCodeChunker"),
        ("ruby", "RubyCodeChunker"),
        ("rb", "RubyCodeChunker"),
        ("javascript", "JsTsCodeChunker"),
        ("js", "JsTsCodeChunker"),
        ("typescript", "JsTsCodeChunker"),
        ("ts", "JsTsCodeChunker"),
    ],
)
def test_create_chunker_uses_language_registry(language, class_name):
    chunker = create_chunker(
        language,
        max_lines_per_chunk=42,
        chunk_depth=1,
        include_header_epilogue=True,
        l2_level_exclusive=False,
        skeleton_mode=True,
        include_l2_in_file_skeleton=False,
    )

    assert chunker.__class__.__name__ == class_name
    assert chunker.max_lines_per_chunk == 42
    assert chunker.chunk_depth == 1
    assert chunker.include_header_epilogue is True
    assert chunker.l2_level_exclusive is False
    assert chunker.skeleton_mode is True
    assert chunker.include_l2_in_file_skeleton is False


def test_create_chunker_rejects_unsupported_language():
    with pytest.raises(ValueError, match=r"Unsupported language: c\. Supported:"):
        create_chunker("c")
