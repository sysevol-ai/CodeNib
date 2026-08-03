# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the PHP tree-sitter chunker."""

from __future__ import annotations

from codenib.code_chunker import CodeChunker, RepoChunkingConfig

PHP_SAMPLE = """<?php

namespace Billing\\Core;

interface Printable {
    public function print(): void;
}

trait Timestamped {
    public function touch(): void {}
}

enum Status {
    case Draft;

    public function label(): string {
        return $this->name;
    }
}

class Invoice implements Printable {
    use Timestamped;

    public int $total = 0;

    public function __construct(int $total) {
        $this->total = $total;
    }

    public function total(): int {
        return $this->total;
    }

    public static function create(int $total): self {
        return new self($total);
    }
}

function normalize(string $value): string {
    return trim($value);
}
"""


def test_php_chunker_level_one_types_and_functions(tmp_path):
    src = tmp_path / "src" / "Billing" / "Invoice.php"
    src.parent.mkdir(parents=True)
    src.write_text(PHP_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="php", chunk_depth=1)
    chunks = chunker.chunk_file(str(src), relative_path="src/Billing/Invoice.php")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("interface", "Printable"),
        ("trait", "Timestamped"),
        ("enum", "Status"),
        ("class", "Invoice"),
        ("function", "normalize"),
    ]
    assert chunks[3].node_id == "src/Billing/Invoice.php:Invoice"
    assert chunks[-1].node_id == "src/Billing/Invoice.php:normalize()"


def test_php_chunker_level_two_members(tmp_path):
    src = tmp_path / "Invoice.php"
    src.write_text(PHP_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="php")
    chunks = chunker.chunk_file(str(src), relative_path="Invoice.php")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("method", "Printable.print"),
        ("method", "Timestamped.touch"),
        ("method", "Status.label"),
        ("property", "Invoice.total"),
        ("method", "Invoice.__construct"),
        ("method", "Invoice.total"),
        ("method", "Invoice.create"),
        ("function", "normalize"),
    ]
    assert "public function total(): int" in chunks[5].content
    assert chunks[3].node_id == "Invoice.php:Invoice.total"
    assert chunks[5].node_id == "Invoice.php:Invoice.total()"


def test_php_chunker_file_skeleton_includes_member_signatures(tmp_path):
    src = tmp_path / "Invoice.php"
    src.write_text(PHP_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="php", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(src), relative_path="Invoice.php")

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "file"
    assert "interface Printable" in chunks[0].content
    assert "public function print(): void;" in chunks[0].content
    assert "enum Status" in chunks[0].content
    assert "public int $total = 0;" in chunks[0].content
    assert "function normalize(string $value): string" in chunks[0].content


def test_php_repo_chunking_discovery(tmp_path):
    src = tmp_path / "src" / "App.php"
    src.parent.mkdir(parents=True)
    src.write_text("<?php\nclass App { public function run() {} }\n", encoding="utf-8")

    cfg = RepoChunkingConfig(languages=["php"], filter_tests=False)
    chunker = CodeChunker(language="php", repo_config=cfg)

    chunks = chunker.chunk_repository(str(tmp_path))

    assert [chunk.name for chunk in chunks] == ["App.run"]
    assert chunks[0].node_id == "src/App.php:App.run()"
