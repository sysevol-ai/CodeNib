# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Swift tree-sitter chunker."""

from __future__ import annotations

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig

SWIFT_SAMPLE = """import Foundation

protocol Printable {
    func printValue()
}

struct Invoice {
    let amount: Int

    func total() -> Int {
        amount
    }
}

func normalize(_ value: String) -> String {
    value.trimmingCharacters(in: .whitespaces)
}
"""


def test_swift_chunker_level_one_containers_and_functions(tmp_path):
    src = tmp_path / "Sources" / "Billing" / "Invoice.swift"
    src.parent.mkdir(parents=True)
    src.write_text(SWIFT_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="swift", chunk_depth=1)
    chunks = chunker.chunk_file(str(src), relative_path="Sources/Billing/Invoice.swift")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("protocol", "Printable"),
        ("struct", "Invoice"),
        ("function", "normalize"),
    ]
    assert chunks[1].node_id == "Sources/Billing/Invoice.swift:Invoice"


def test_swift_chunker_level_two_members(tmp_path):
    src = tmp_path / "Invoice.swift"
    src.write_text(SWIFT_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="swift")
    chunks = chunker.chunk_file(str(src), relative_path="Invoice.swift")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("method", "Printable.printValue"),
        ("property", "Invoice.amount"),
        ("method", "Invoice.total"),
        ("function", "normalize"),
    ]
    assert "func total() -> Int" in chunks[2].content
    assert chunks[2].node_id == "Invoice.swift:Invoice.total()"


def test_swift_chunker_file_skeleton_includes_member_signatures(tmp_path):
    src = tmp_path / "Invoice.swift"
    src.write_text(SWIFT_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="swift", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(src), relative_path="Invoice.swift")

    assert len(chunks) == 1
    assert "protocol Printable" in chunks[0].content
    assert "func printValue()" in chunks[0].content
    assert "struct Invoice" in chunks[0].content
    assert "let amount: Int" in chunks[0].content
    assert "func normalize(_ value: String) -> String" in chunks[0].content


def test_swift_repo_chunking_discovery(tmp_path):
    src = tmp_path / "Sources" / "App.swift"
    src.parent.mkdir(parents=True)
    src.write_text("struct App { func run() {} }\n", encoding="utf-8")

    cfg = RepoChunkingConfig(languages=["swift"], filter_tests=False)
    chunker = CodeChunker(language="swift", repo_config=cfg)

    chunks = chunker.chunk_repository(str(tmp_path))

    assert [chunk.name for chunk in chunks] == ["App.run"]
    assert chunks[0].node_id == "Sources/App.swift:App.run()"
