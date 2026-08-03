# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Ruby tree-sitter chunker."""

from __future__ import annotations

from codenib.code_chunker import CodeChunker, RepoChunkingConfig

RUBY_SAMPLE = """# frozen_string_literal: true

module Billing
  class Invoice
    def initialize(total)
      @total = total
    end

    def total
      @total
    end

    def self.build(total)
      new(total)
    end

    class LineItem
      def amount
        42
      end
    end
  end

  def self.normalize(value)
    value.to_s
  end
end

def top_level(value)
  value
end
"""


def test_ruby_chunker_level_one_namespaces_and_top_level_methods(tmp_path):
    src = tmp_path / "lib" / "billing" / "invoice.rb"
    src.parent.mkdir(parents=True)
    src.write_text(RUBY_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="ruby", chunk_depth=1)
    chunks = chunker.chunk_file(str(src), relative_path="lib/billing/invoice.rb")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("module", "Billing"),
        ("function", "top_level"),
    ]
    assert chunks[0].node_id == "lib/billing/invoice.rb:Billing"
    assert chunks[1].node_id == "lib/billing/invoice.rb:top_level()"


def test_ruby_chunker_level_two_methods(tmp_path):
    src = tmp_path / "invoice.rb"
    src.write_text(RUBY_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="ruby")
    chunks = chunker.chunk_file(str(src), relative_path="invoice.rb")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("method", "Billing.Invoice.initialize"),
        ("method", "Billing.Invoice.total"),
        ("method", "Billing.Invoice.build"),
        ("method", "Billing.Invoice.LineItem.amount"),
        ("method", "Billing.normalize"),
        ("function", "top_level"),
    ]
    assert "def total" in chunks[1].content
    assert chunks[1].node_id == "invoice.rb:Billing.Invoice.total()"


def test_ruby_chunker_file_skeleton_includes_member_signatures(tmp_path):
    src = tmp_path / "invoice.rb"
    src.write_text(RUBY_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="ruby", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(src), relative_path="invoice.rb")

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "file"
    assert "module Billing" in chunks[0].content
    assert "def initialize(total)" in chunks[0].content
    assert "def self.build(total)" in chunks[0].content
    assert "def self.normalize(value)" in chunks[0].content
    assert "def top_level(value)" in chunks[0].content


def test_ruby_repo_chunking_discovery(tmp_path):
    src = tmp_path / "lib" / "app.rb"
    src.parent.mkdir(parents=True)
    src.write_text("class App\n  def run\n  end\nend\n", encoding="utf-8")

    cfg = RepoChunkingConfig(languages=["ruby"], filter_tests=False)
    chunker = CodeChunker(language="ruby", repo_config=cfg)

    chunks = chunker.chunk_repository(str(tmp_path))

    assert [chunk.name for chunk in chunks] == ["App.run"]
    assert chunks[0].node_id == "lib/app.rb:App.run()"
