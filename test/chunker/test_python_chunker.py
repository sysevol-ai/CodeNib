#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for the Python code chunker using the httpie CLI repository.
"""

from pathlib import Path

import pytest

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig

pytestmark = pytest.mark.integration


def _collect_chunks(repo_root: Path, chunk_depth: int, l2_level_exclusive: bool = True):
    repo_config = RepoChunkingConfig(languages=["python"])
    chunker = CodeChunker(
        language="python",
        repo_config=repo_config,
        max_lines_per_chunk=None,
        chunk_depth=chunk_depth,
        l2_level_exclusive=l2_level_exclusive,
    )
    return chunker.chunk_repository(str(repo_root))


def test_python_chunker_level_zero(httpie_cli_repo):
    chunks = _collect_chunks(httpie_cli_repo, chunk_depth=0)
    assert chunks, "Python chunker returned no chunks at depth=0"
    assert all(chunk.chunk_type == "file" for chunk in chunks)


def test_python_chunker_top_level_only(httpie_cli_repo):
    chunks = _collect_chunks(httpie_cli_repo, chunk_depth=1)
    chunk_types = {chunk.chunk_type for chunk in chunks}
    assert "function" in chunk_types
    assert "method" not in chunk_types


def test_python_chunker_includes_methods(httpie_cli_repo):
    chunks = _collect_chunks(httpie_cli_repo, chunk_depth=2)
    chunk_types = {chunk.chunk_type for chunk in chunks}
    assert "method" in chunk_types


def test_python_chunker_includes_classes_when_enabled(httpie_cli_repo):
    chunks = _collect_chunks(httpie_cli_repo, chunk_depth=2, l2_level_exclusive=False)
    chunk_types = {chunk.chunk_type for chunk in chunks}
    assert "class" in chunk_types


def test_python_chunker_chunk_file(httpie_cli_repo):
    target_file = Path(httpie_cli_repo) / "httpie" / "core.py"
    assert target_file.exists(), f"Target Python file not found: {target_file}"

    chunker = CodeChunker(language="python", chunk_depth=2)
    chunks = chunker.chunk_file(str(target_file))

    assert chunks, "Chunker did not return any chunks for httpie/core.py"
    assert any(chunk.chunk_type == "function" for chunk in chunks)
    assert any(chunk.name == "raw_main" for chunk in chunks)


def _first_symbols_from_file(path: Path):
    """Grab representative symbols from a repository file for assertions."""
    module_func = None
    class_name = None
    method_name = None
    method_body_line = None

    lines = path.read_text().splitlines()
    in_class = False
    class_indent = None
    method_indent = None

    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not stripped:
            continue

        if module_func is None and stripped.startswith(("def ", "async def ")):
            module_func = stripped.split()[1].split("(")[0]

        if not in_class and stripped.startswith("class "):
            class_name = stripped.split()[1].split("(")[0].rstrip(":")
            in_class = True
            class_indent = indent
            continue

        if in_class:
            if indent <= class_indent:
                in_class = False
                class_indent = None
                method_indent = None
                continue
            if method_name is None and stripped.startswith(("def ", "async def ")):
                method_name = stripped.split()[1].split("(")[0]
                method_indent = indent
                # Grab the first body line nested under the method
                for tail in lines[idx + 1 :]:
                    tail_stripped = tail.lstrip()
                    if not tail_stripped:
                        continue
                    tail_indent = len(tail) - len(tail_stripped)
                    if tail_indent <= method_indent:
                        break
                    method_body_line = tail_stripped
                    break
    return module_func, class_name, method_name, method_body_line


def test_python_chunker_skeleton_mode_repository_file(httpie_cli_repo):
    target_file = Path(httpie_cli_repo) / "httpie" / "manager" / "compat.py"
    if not target_file.exists():
        pytest.skip(f"Target file missing in fixture repo: {target_file}")

    _, class_name, method_name, method_body_line = _first_symbols_from_file(target_file)
    assert class_name and method_name, "Expected class and method in compat.py"

    chunker = CodeChunker(
        language="python",
        chunk_depth=2,
        l2_level_exclusive=False,
    )
    chunks = chunker.chunk_file(str(target_file), skeleton_mode=True)

    class_chunk = next(
        chunk
        for chunk in chunks
        if chunk.chunk_type == "class" and chunk.name == class_name
    )
    assert class_name in class_chunk.content
    assert f"def {method_name}" in class_chunk.content
    if method_body_line:
        assert method_body_line not in class_chunk.content

    method_chunk = next(
        chunk
        for chunk in chunks
        if chunk.chunk_type == "method" and chunk.name.endswith(f".{method_name}")
    )
    assert f"def {method_name}" in method_chunk.content
    if method_body_line:
        assert method_body_line not in method_chunk.content


def test_python_file_skeleton_includes_l2_repository_file(httpie_cli_repo):
    target_file = Path(httpie_cli_repo) / "httpie" / "manager" / "compat.py"
    if not target_file.exists():
        pytest.skip(f"Target file missing in fixture repo: {target_file}")

    module_func, class_name, method_name, _ = _first_symbols_from_file(target_file)
    chunker = CodeChunker(
        language="python",
        chunk_depth=0,
        skeleton_mode=True,
    )
    chunks = chunker.chunk_file(str(target_file))

    assert len(chunks) == 1
    content = chunks[0].content

    if module_func:
        assert f"def {module_func}" in content
    assert class_name and class_name in content
    assert method_name and f"def {method_name}" in content
