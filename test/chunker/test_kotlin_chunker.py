# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Kotlin tree-sitter chunker."""

from __future__ import annotations

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig

KOTLIN_SAMPLE = """package com.example

interface Runnable {
    fun run()
}

object Config {
    val enabled: Boolean = true
    fun load() = enabled
}

data class Point(val x: Int, val y: Int) {
    val sum: Int
        get() = x + y

    constructor(x: Int) : this(x, 0)

    fun move(dx: Int): Point {
        return Point(x + dx, y)
    }

    companion object {
        fun origin(): Point = Point(0, 0)
    }
}

enum class Status {
    ACTIVE;

    fun label(): String = name.lowercase()
}

const val DEFAULT_LIMIT = 10

fun normalize(value: String): String {
    return value.trim()
}
"""


def test_kotlin_chunker_level_one_containers_and_top_level_symbols(tmp_path):
    src = tmp_path / "src" / "main" / "kotlin" / "com" / "example" / "Point.kt"
    src.parent.mkdir(parents=True)
    src.write_text(KOTLIN_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="kotlin", chunk_depth=1)
    chunks = chunker.chunk_file(str(src), relative_path="src/main/kotlin/Point.kt")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("interface", "Runnable"),
        ("object", "Config"),
        ("class", "Point"),
        ("enum", "Status"),
        ("property", "DEFAULT_LIMIT"),
        ("function", "normalize"),
    ]
    assert chunks[2].node_id == "src/main/kotlin/Point.kt:Point"


def test_kotlin_chunker_level_two_members(tmp_path):
    src = tmp_path / "Point.kt"
    src.write_text(KOTLIN_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="kt")
    chunks = chunker.chunk_file(str(src), relative_path="Point.kt")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("method", "Runnable.run"),
        ("property", "Config.enabled"),
        ("method", "Config.load"),
        ("property", "Point.x"),
        ("property", "Point.y"),
        ("property", "Point.sum"),
        ("method", "Point.Point"),
        ("method", "Point.move"),
        ("method", "Point.Companion.origin"),
        ("method", "Status.label"),
        ("property", "DEFAULT_LIMIT"),
        ("function", "normalize"),
    ]
    assert "fun move(dx: Int): Point" in chunks[7].content
    assert chunks[3].node_id == "Point.kt:Point.x"
    assert chunks[5].node_id == "Point.kt:Point.sum"
    assert chunks[6].node_id == "Point.kt:Point.Point()"


def test_kotlin_chunker_file_skeleton_includes_member_signatures(tmp_path):
    src = tmp_path / "Point.kt"
    src.write_text(KOTLIN_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="kotlin", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(src), relative_path="Point.kt")

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "file"
    assert "interface Runnable" in chunks[0].content
    assert "fun run()" in chunks[0].content
    assert "object Config" in chunks[0].content
    assert "data class Point(val x: Int, val y: Int)" in chunks[0].content
    assert "val x: Int" in chunks[0].content
    assert "val y: Int" in chunks[0].content
    assert "constructor(x: Int) : this(x, 0)" in chunks[0].content
    assert "enum class Status" in chunks[0].content
    assert "const val DEFAULT_LIMIT" in chunks[0].content
    assert "fun normalize(value: String): String" in chunks[0].content


def test_kotlin_repo_chunking_discovery(tmp_path):
    src = tmp_path / "src" / "main" / "kotlin" / "App.kt"
    src.parent.mkdir(parents=True)
    src.write_text("class App { fun run() {} }\n", encoding="utf-8")

    cfg = RepoChunkingConfig(languages=["kt"], filter_tests=False)
    chunker = CodeChunker(language="kotlin", repo_config=cfg)

    chunks = chunker.chunk_repository(str(tmp_path))

    assert [chunk.name for chunk in chunks] == ["App.run"]
    assert chunks[0].node_id == "src/main/kotlin/App.kt:App.run()"
