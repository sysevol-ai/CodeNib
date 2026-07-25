# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Scala tree-sitter chunker."""

from __future__ import annotations

from codenib.code_chunker import CodeChunker, RepoChunkingConfig

SCALA_SAMPLE = """package app

trait Printable {
  def printValue(): Unit
}

class Invoice(val amount: Int) {
  def total(): Int = amount
}

object Util {
  def normalize(value: String): String = value.trim
}
"""


def test_scala_chunker_level_one_containers(tmp_path):
    src = tmp_path / "src" / "main" / "scala" / "Invoice.scala"
    src.parent.mkdir(parents=True)
    src.write_text(SCALA_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="scala", chunk_depth=1)
    chunks = chunker.chunk_file(str(src), relative_path="src/main/scala/Invoice.scala")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("trait", "Printable"),
        ("class", "Invoice"),
        ("object", "Util"),
    ]
    assert chunks[1].node_id == "src/main/scala/Invoice.scala:Invoice"


def test_scala_chunker_level_two_members(tmp_path):
    src = tmp_path / "Invoice.scala"
    src.write_text(SCALA_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="scala")
    chunks = chunker.chunk_file(str(src), relative_path="Invoice.scala")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("method", "Printable.printValue"),
        ("property", "Invoice.amount"),
        ("method", "Invoice.total"),
        ("method", "Util.normalize"),
    ]
    assert "def total(): Int" in chunks[2].content
    assert chunks[2].node_id == "Invoice.scala:Invoice.total()"


def test_scala_chunker_file_skeleton_includes_member_signatures(tmp_path):
    src = tmp_path / "Invoice.scala"
    src.write_text(SCALA_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="scala", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(src), relative_path="Invoice.scala")

    assert len(chunks) == 1
    assert "trait Printable" in chunks[0].content
    assert "def printValue(): Unit" in chunks[0].content
    assert "class Invoice(val amount: Int)" in chunks[0].content
    assert "val amount: Int" in chunks[0].content
    assert "object Util" in chunks[0].content


def test_scala_repo_chunking_discovery(tmp_path):
    src = tmp_path / "src" / "main" / "scala" / "App.scala"
    src.parent.mkdir(parents=True)
    src.write_text("class App { def run(): Unit = () }\n", encoding="utf-8")

    cfg = RepoChunkingConfig(languages=["scala"], filter_tests=False)
    chunker = CodeChunker(language="scala", repo_config=cfg)

    chunks = chunker.chunk_repository(str(tmp_path))

    assert [chunk.name for chunk in chunks] == ["App.run"]
    assert chunks[0].node_id == "src/main/scala/App.scala:App.run()"
