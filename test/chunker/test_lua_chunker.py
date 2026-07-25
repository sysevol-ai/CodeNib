# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Lua tree-sitter chunker."""

from __future__ import annotations

from codenib.code_chunker import CodeChunker, RepoChunkingConfig

LUA_SAMPLE = """local M = {}

function M.total(amount)
  return amount
end

local function normalize(value)
  return tostring(value)
end

M.format = function(value)
  return tostring(value)
end

return M
"""


def test_lua_chunker_extracts_functions(tmp_path):
    src = tmp_path / "lua" / "invoice.lua"
    src.parent.mkdir(parents=True)
    src.write_text(LUA_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="lua")
    chunks = chunker.chunk_file(str(src), relative_path="lua/invoice.lua")

    assert [(chunk.chunk_type, chunk.name) for chunk in chunks] == [
        ("function", "M.total"),
        ("function", "normalize"),
        ("function", "M.format"),
    ]
    assert chunks[0].node_id == "lua/invoice.lua:M.total()"
    assert "function M.total(amount)" in chunks[0].content


def test_lua_chunker_file_skeleton_includes_function_signatures(tmp_path):
    src = tmp_path / "invoice.lua"
    src.write_text(LUA_SAMPLE, encoding="utf-8")

    chunker = CodeChunker(language="lua", chunk_depth=0, skeleton_mode=True)
    chunks = chunker.chunk_file(str(src), relative_path="invoice.lua")

    assert len(chunks) == 1
    assert "function M.total(amount)" in chunks[0].content
    assert "local function normalize(value)" in chunks[0].content
    assert "function(value)" in chunks[0].content


def test_lua_repo_chunking_discovery(tmp_path):
    src = tmp_path / "src" / "app.lua"
    src.parent.mkdir(parents=True)
    src.write_text("function run() end\n", encoding="utf-8")

    cfg = RepoChunkingConfig(languages=["luau"], filter_tests=False)
    chunker = CodeChunker(language="luau", repo_config=cfg)

    chunks = chunker.chunk_repository(str(tmp_path))

    assert [chunk.name for chunk in chunks] == ["run"]
    assert chunks[0].node_id == "src/app.lua:run()"
