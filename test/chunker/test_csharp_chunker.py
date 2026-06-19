# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the C# tree-sitter chunker."""

from __future__ import annotations

from codeminer.code_chunker import CodeChunker, RepoChunkingConfig

C_SHARP_SAMPLE = """using System;

namespace Billing.Core;

public interface IInvoice {
    decimal Total { get; }
    void Print();
}

public enum InvoiceStatus {
    Draft,
    Paid
}

public record Money(decimal Amount, string Currency);

public class Invoice {
    private decimal _total;

    public Invoice(decimal total) {
        _total = total;
    }

    public decimal Total => _total;

    public void AddLine(decimal amount) {
        _total += amount;
    }

    public static Invoice Create(decimal total) {
        return new Invoice(total);
    }

    public class LineItem {
        public decimal Amount { get; init; }
    }
}

public struct Point {
    public int X;
}

static void TopLevelPreview() {}
"""


def test_csharp_chunker_level_one_types_and_top_level_function(tmp_path):
    src = tmp_path / "src" / "Billing" / "Invoice.cs"
    src.parent.mkdir(parents=True)
    src.write_text(C_SHARP_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="csharp", chunk_depth=1)
    chunks = chunker.chunk_file(str(src), relative_path="src/Billing/Invoice.cs")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("interface", "IInvoice"),
        ("enum", "InvoiceStatus"),
        ("record", "Money"),
        ("class", "Invoice"),
        ("struct", "Point"),
        ("function", "TopLevelPreview"),
    ]
    assert chunks[3].node_id == "src/Billing/Invoice.cs:Invoice"
    assert chunks[-1].node_id == "src/Billing/Invoice.cs:TopLevelPreview()"


def test_csharp_chunker_level_two_members(tmp_path):
    src = tmp_path / "Invoice.cs"
    src.write_text(C_SHARP_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="csharp")
    chunks = chunker.chunk_file(str(src), relative_path="Invoice.cs")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("property", "IInvoice.Total"),
        ("method", "IInvoice.Print"),
        ("method", "Invoice.Invoice"),
        ("property", "Invoice.Total"),
        ("method", "Invoice.AddLine"),
        ("method", "Invoice.Create"),
        ("property", "Invoice.LineItem.Amount"),
        ("function", "TopLevelPreview"),
    ]
    assert "public void AddLine(decimal amount)" in chunks[4].content
    assert chunks[3].node_id == "Invoice.cs:Invoice.Total"
    assert chunks[4].node_id == "Invoice.cs:Invoice.AddLine()"


def test_csharp_chunker_file_skeleton_includes_member_signatures(tmp_path):
    src = tmp_path / "Invoice.cs"
    src.write_text(C_SHARP_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="csharp", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(src), relative_path="Invoice.cs")

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "file"
    assert "public interface IInvoice" in chunks[0].content
    assert "decimal Total" in chunks[0].content
    assert "public record Money(decimal Amount, string Currency);" in chunks[0].content
    assert "public void AddLine(decimal amount)" in chunks[0].content
    assert "static void TopLevelPreview()" in chunks[0].content


def test_csharp_repo_chunking_discovery(tmp_path):
    src = tmp_path / "src" / "App.cs"
    src.parent.mkdir(parents=True)
    src.write_text("class App { void Run() {} }\n", encoding="utf-8")

    cfg = RepoChunkingConfig(languages=["csharp"], filter_tests=False)
    chunker = CodeChunker(language="csharp", repo_config=cfg)

    chunks = chunker.chunk_repository(str(tmp_path))

    assert [chunk.name for chunk in chunks] == ["App.Run"]
    assert chunks[0].node_id == "src/App.cs:App.Run()"
