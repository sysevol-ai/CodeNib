#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for shared code chunker behavior."""

from types import SimpleNamespace
from typing import List, Optional, Tuple

import pytest

from codenib.code_chunking.base import (
    DEFAULT_L0_RAW_FALLBACK_MAX_LINES,
    BaseCodeChunker,
)


class StubCodeChunker(BaseCodeChunker):
    """Minimal concrete chunker for base-class tests."""

    def _build_file_skeleton(
        self,
        definitions: List[Tuple],
        code_content: str,
        include_l2: Optional[bool] = None,
    ) -> str:
        return ""

    def _find_top_level_definitions(
        self, root_node, include_l2_in_file_skeleton: bool = False
    ) -> List[Tuple]:
        return []

    def _extract_signature_text(self, node, def_type: str, code_content: str) -> str:
        return ""

    def _extract_function_name(self, node) -> Optional[str]:
        return None

    def _extract_class_name(self, node) -> Optional[str]:
        return None


@pytest.fixture(autouse=True)
def clear_language_cache():
    BaseCodeChunker._language_cache.clear()
    yield
    BaseCodeChunker._language_cache.clear()


def test_tree_sitter_language_load_is_cached_per_language(monkeypatch):
    calls = []
    languages = {"python": object(), "go": object()}

    def fake_get_language(language: str):
        calls.append(language)
        return languages[language]

    monkeypatch.setattr("codenib.code_chunking.base.get_language", fake_get_language)
    monkeypatch.setattr(
        BaseCodeChunker,
        "_create_parser",
        staticmethod(lambda language: object()),
    )

    first = StubCodeChunker("python")
    second = StubCodeChunker("python")
    third = StubCodeChunker("go")

    assert calls == ["python", "go"]
    assert first.tree_sitter_language is second.tree_sitter_language
    assert third.tree_sitter_language is languages["go"]


def test_tree_sitter_language_reports_first_use_download(monkeypatch):
    messages = []
    monkeypatch.setattr(
        "codenib.code_chunking.base.logger.info",
        lambda message, *args: messages.append(message % args),
    )
    monkeypatch.setattr("codenib.code_chunking.base.downloaded_languages", lambda: [])
    monkeypatch.setattr(
        "codenib.code_chunking.base.get_language", lambda _language: object()
    )
    monkeypatch.setattr(
        BaseCodeChunker,
        "_create_parser",
        staticmethod(lambda _language: object()),
    )

    StubCodeChunker("python")

    assert any(
        "Preparing the python tree-sitter parser for first use" in message
        for message in messages
    )


def test_tree_sitter_language_failure_raises_instead_of_exiting(monkeypatch):
    monkeypatch.setattr("codenib.code_chunking.base.downloaded_languages", lambda: [])

    def fail_load(_language: str):
        raise OSError("offline")

    monkeypatch.setattr("codenib.code_chunking.base.get_language", fail_load)

    with pytest.raises(RuntimeError, match="downloaded and cached on first use"):
        StubCodeChunker("python")


def test_l0_empty_skeleton_falls_back_to_full_file(monkeypatch, tmp_path):
    source = tmp_path / "declarations.h"
    content = "#define VALUE 1\n"
    source.write_text(content, encoding="utf-8")

    parser = SimpleNamespace(parse=lambda _code: SimpleNamespace(root_node=object()))
    monkeypatch.setattr(
        "codenib.code_chunking.base.get_language", lambda _lang: object()
    )
    monkeypatch.setattr(
        BaseCodeChunker,
        "_create_parser",
        staticmethod(lambda _language: parser),
    )

    chunker = StubCodeChunker("cpp", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(source), relative_path="include/declarations.h")

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "file"
    assert chunks[0].content == f"include/declarations.h\n{content}"
    assert chunks[0].node_id == "include/declarations.h"


def test_chunk_file_replaces_invalid_utf8_bytes(monkeypatch, tmp_path):
    source = tmp_path / "legacy.py"
    source.write_bytes(b"value = b\x80\n")

    parser = SimpleNamespace(parse=lambda _code: SimpleNamespace(root_node=object()))
    monkeypatch.setattr(
        "codenib.code_chunking.base.get_language", lambda _lang: object()
    )
    monkeypatch.setattr(
        BaseCodeChunker,
        "_create_parser",
        staticmethod(lambda _language: parser),
    )

    chunker = StubCodeChunker("python", chunk_depth=0)
    chunks = chunker.chunk_file(str(source), relative_path="legacy.py")

    assert len(chunks) == 1
    assert "\ufffd" in chunks[0].content


@pytest.mark.parametrize(
    ("configured_limit", "expected_limit"),
    [(None, DEFAULT_L0_RAW_FALLBACK_MAX_LINES), (100, 100)],
)
def test_l0_empty_skeleton_fallback_is_bounded(
    monkeypatch, tmp_path, configured_limit, expected_limit
):
    source = tmp_path / "generated.h"
    source.write_text(
        "".join(f"#define VALUE_{line} {line}\n" for line in range(5000)),
        encoding="utf-8",
    )

    parser = SimpleNamespace(parse=lambda _code: SimpleNamespace(root_node=object()))
    monkeypatch.setattr(
        "codenib.code_chunking.base.get_language", lambda _lang: object()
    )
    monkeypatch.setattr(
        BaseCodeChunker,
        "_create_parser",
        staticmethod(lambda _language: parser),
    )

    chunker = StubCodeChunker(
        "cpp",
        max_lines_per_chunk=configured_limit,
        chunk_depth=0,
        skeleton_mode=True,
    )
    chunks = chunker.chunk_file(str(source), relative_path="include/generated.h")

    assert len(chunks) == (5001 + expected_limit - 1) // expected_limit
    assert all(
        chunk.end_line - chunk.start_line + 1 <= expected_limit for chunk in chunks
    )
    assert all(chunk.node_id == "include/generated.h" for chunk in chunks)
