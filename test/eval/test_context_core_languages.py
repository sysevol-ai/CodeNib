# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Adapt dataset labels to existing registry-backed index chunkers."""

import pytest

from codenib.code_chunker import CodeChunker, RepoChunkingConfig
from scripts.benchmark_context_core import index_languages


@pytest.mark.parametrize(
    "label,expected",
    [
        ("c", ["cpp"]),
        ("C", ["cpp"]),
        ("c++", ["cpp"]),
        ("js", ["javascript", "typescript"]),
        ("typescript", ["javascript", "typescript"]),
        ("python", ["python"]),
    ],
)
def test_index_language_labels_use_registry_contract(label, expected):
    assert index_languages(label) == expected


def test_c_dataset_label_chunks_actual_c_source_without_changing_factory(tmp_path):
    source = tmp_path / "answer.c"
    source.write_text("int answer(void) {\n  return 42;\n}\n")
    languages = index_languages("c")
    chunker = CodeChunker(
        language=languages[0],
        repo_config=RepoChunkingConfig(languages=languages, filter_tests=False),
    )
    chunks = chunker.chunk_repository(str(tmp_path), strict=True)
    assert any(
        c.name == "answer"
        and c.chunk_type == "function"
        and c.start_line == 0
        and c.end_line == 2
        for c in chunks
    )
