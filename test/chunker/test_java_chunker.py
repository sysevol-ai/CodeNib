# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Java tree-sitter chunker."""

from __future__ import annotations

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig

JAVA_SAMPLE = """package com.example;

public class Greeter {
    private final String name;

    public Greeter(String name) {
        this.name = name;
    }

    public String greet(String target) {
        return "Hello " + target;
    }

    static class Nested {
        void run() {}
    }
}

interface Runner {
    void run();
}

enum Mode {
    FAST,
    SLOW;

    boolean isFast() {
        return this == FAST;
    }
}

record Point(int x, int y) {
    public int sum() {
        return x + y;
    }
}
"""


def test_java_chunker_level_one_containers(tmp_path):
    src = tmp_path / "src" / "main" / "java" / "com" / "example" / "Greeter.java"
    src.parent.mkdir(parents=True)
    src.write_text(JAVA_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="java", chunk_depth=1)
    chunks = chunker.chunk_file(str(src), relative_path="src/main/java/Greeter.java")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("class", "Greeter"),
        ("interface", "Runner"),
        ("enum", "Mode"),
        ("record", "Point"),
    ]
    assert chunks[0].node_id == "src/main/java/Greeter.java:Greeter"


def test_java_chunker_level_two_methods(tmp_path):
    src = tmp_path / "Greeter.java"
    src.write_text(JAVA_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="java")
    chunks = chunker.chunk_file(str(src), relative_path="Greeter.java")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("method", "Greeter.Greeter"),
        ("method", "Greeter.greet"),
        ("method", "Greeter.Nested.run"),
        ("method", "Runner.run"),
        ("method", "Mode.isFast"),
        ("method", "Point.sum"),
    ]
    assert "public String greet(String target)" in chunks[1].content
    assert chunks[1].node_id == "Greeter.java:Greeter.greet()"


def test_java_chunker_file_skeleton_includes_member_signatures(tmp_path):
    src = tmp_path / "Greeter.java"
    src.write_text(JAVA_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="java", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(src), relative_path="Greeter.java")

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "file"
    assert "public class Greeter" in chunks[0].content
    assert "public Greeter(String name)" in chunks[0].content
    assert "interface Runner" in chunks[0].content
    assert "record Point(int x, int y)" in chunks[0].content


def test_java_repo_chunking_discovery(tmp_path):
    src = tmp_path / "src" / "main" / "java" / "App.java"
    src.parent.mkdir(parents=True)
    src.write_text("class App { void run() {} }\n", encoding="utf-8")

    cfg = RepoChunkingConfig(languages=["java"], filter_tests=False)
    chunker = CodeChunker(language="java", repo_config=cfg)

    chunks = chunker.chunk_repository(str(tmp_path))

    assert [chunk.name for chunk in chunks] == ["App.run"]
    assert chunks[0].node_id == "src/main/java/App.java:App.run()"
