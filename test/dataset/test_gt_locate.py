#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for codeminer.dataset.gt_locate.

Unit tests run without network access (patch parsing, helpers).
Integration tests pull real repos from SWE-bench Multilingual and exercise
the full analyze_instance pipeline.
"""

import pytest

from codeminer.code_chunking.base import CodeChunk
from codeminer.dataset.gt_locate import (
    GTLocator,
    _chunk_to_code_block,
    language_for_file,
)

# ===================================================================
# 1. language_for_file — extension → chunker language mapping
# ===================================================================


class TestLanguageForFile:
    @pytest.mark.parametrize(
        "path, expected",
        [
            ("src/main.py", "python"),
            ("cmd/server.go", "go"),
            ("lib/parser.rs", "rust"),
            ("src/engine.cpp", "cpp"),
            ("include/engine.h", "cpp"),
            ("src/app.ts", "typescript"),
            ("src/index.js", "javascript"),
            ("src/component.tsx", "typescript"),
            ("src/component.jsx", "javascript"),
            ("README.md", None),
            ("Makefile", None),
            ("data.json", None),
        ],
    )
    def test_extension_mapping(self, path, expected):
        assert language_for_file(path) == expected


# ===================================================================
# 2. _chunk_to_code_block — 0-based → 1-based conversion
# ===================================================================


class TestChunkToCodeBlock:
    def test_basic_conversion(self):
        chunk = CodeChunk(
            content="def foo():\n    pass",
            start_line=9,  # 0-based
            end_line=10,  # 0-based
            chunk_type="function",
            name="foo",
            file="/repo/src/utils.py",
            node_id="src/utils.py:foo()",
        )
        block = _chunk_to_code_block(chunk, "modified")
        assert block["file_path"] == "src/utils.py"
        assert block["symbol"] == "foo()"
        assert block["start_line"] == 10  # 1-based
        assert block["end_line"] == 11  # 1-based
        assert block["symbol_type"] == "function"
        assert block["change_type"] == "modified"

    def test_method_conversion(self):
        chunk = CodeChunk(
            content="def bar(self):\n    pass",
            start_line=19,
            end_line=20,
            chunk_type="method",
            name="MyClass.bar",
            file="/repo/src/models.py",
            node_id="src/models.py:MyClass.bar()",
        )
        block = _chunk_to_code_block(chunk, "added")
        assert block["file_path"] == "src/models.py"
        assert block["symbol"] == "MyClass.bar()"
        assert block["start_line"] == 20
        assert block["end_line"] == 21
        assert block["change_type"] == "added"

    def test_class_conversion(self):
        chunk = CodeChunk(
            content="class Baz:\n    pass",
            start_line=0,
            end_line=1,
            chunk_type="class",
            name="Baz",
            file="/repo/src/core.py",
            node_id="src/core.py:Baz",
        )
        block = _chunk_to_code_block(chunk, "deleted")
        assert block["symbol"] == "Baz"
        assert block["start_line"] == 1
        assert block["end_line"] == 2
        assert block["change_type"] == "deleted"


# ===================================================================
# 3. compare_symbols — unit test with mock CodeChunks
# ===================================================================


class TestCompareSymbols:
    @pytest.fixture()
    def locator(self, tmp_path):
        return GTLocator(work_dir=str(tmp_path))

    @staticmethod
    def _make_chunk(node_id, content, start, end, chunk_type="function"):
        name = node_id.partition(":")[2]
        file_path = node_id.partition(":")[0]
        return CodeChunk(
            content=content,
            start_line=start,
            end_line=end,
            chunk_type=chunk_type,
            name=name,
            file=f"/repo/{file_path}",
            node_id=node_id,
        )

    def test_detect_modified_by_length(self, locator):
        before = {
            "f.py:foo()": self._make_chunk("f.py:foo()", "def foo():\n  pass", 0, 1),
        }
        after = {
            "f.py:foo()": self._make_chunk(
                "f.py:foo()", "def foo():\n  x = 1\n  pass", 0, 2
            ),
        }
        modified, added, deleted = locator.compare_symbols(
            before, after, {"f.py": [(1, 3)]}
        )
        assert len(modified) == 1
        assert modified[0][0] == "f.py:foo()"

    def test_detect_modified_by_content(self, locator):
        before = {
            "f.py:bar()": self._make_chunk(
                "f.py:bar()", "def bar():\n  return 1", 5, 6
            ),
        }
        after = {
            "f.py:bar()": self._make_chunk(
                "f.py:bar()", "def bar():\n  return 2", 5, 6
            ),
        }
        modified, added, deleted = locator.compare_symbols(
            before, after, {"f.py": [(6, 6)]}
        )
        assert len(modified) == 1

    def test_detect_added(self, locator):
        before = {}
        after = {
            "f.py:new_func()": self._make_chunk(
                "f.py:new_func()", "def new_func(): ...", 10, 10
            ),
        }
        modified, added, deleted = locator.compare_symbols(before, after, {})
        assert len(added) == 1
        assert added[0][0] == "f.py:new_func()"

    def test_detect_deleted(self, locator):
        before = {
            "f.py:old()": self._make_chunk("f.py:old()", "def old(): ...", 0, 0),
        }
        after = {}
        modified, added, deleted = locator.compare_symbols(before, after, {})
        assert len(deleted) == 1
        assert deleted[0][0] == "f.py:old()"

    def test_no_change(self, locator):
        chunk = self._make_chunk("f.py:stable()", "def stable(): ...", 5, 5)
        before = {"f.py:stable()": chunk}
        after = {"f.py:stable()": chunk}
        modified, added, deleted = locator.compare_symbols(
            before, after, {"f.py": [(100, 105)]}
        )
        assert modified == []
        assert added == []
        assert deleted == []

    def test_returns_chunk_objects(self, locator):
        """compare_symbols should return (name, CodeChunk) tuples."""
        before = {}
        after = {
            "f.py:greet()": self._make_chunk(
                "f.py:greet()", "def greet(): print('hi')", 0, 0
            ),
        }
        _, added, _ = locator.compare_symbols(before, after, {})
        name, chunk = added[0]
        assert isinstance(chunk, CodeChunk)
        assert chunk.start_line == 0


# ===================================================================
# 4. extract_symbols_from_file — JS/TS variable/assignment functions
# ===================================================================


class TestExtractSymbolsJsTs:
    @pytest.fixture()
    def locator(self, tmp_path):
        return GTLocator(work_dir=str(tmp_path))

    def test_javascript_variable_and_assignment_functions(self, locator, tmp_path):
        js_file = tmp_path / "src" / "expression.js"
        js_file.parent.mkdir(parents=True, exist_ok=True)
        js_file.write_text(
            "\n".join(
                [
                    "export const fromExport = () => 1;",
                    "const fromVar = function () { return 2; };",
                    "exports.fromAssign = () => 3;",
                ]
            ),
            encoding="utf-8",
        )

        symbols = locator.extract_symbols_from_file(
            str(js_file), relative_path="src/expression.js"
        )

        assert "src/expression.js:fromExport()" in symbols
        assert "src/expression.js:fromVar()" in symbols
        assert "src/expression.js:fromAssign()" in symbols


# ===================================================================
# 5. Full pipeline — real SWE-bench Multilingual instances
# ===================================================================
#
# Each test below loads a real instance from the HuggingFace dataset,
# clones the real repository, checks out the base commit, applies the
# real patch, and verifies the extracted code_blocks.
# ===================================================================


def _assert_valid_result(result):
    """Shared assertions for analyze_instance results."""
    assert result["error"] is None, f"analyze_instance failed: {result['error']}"
    assert result["target_files"], "Expected at least one target file"
    assert result["code_blocks"], "Expected at least one code block in ground truth"

    # Validate code_block structure.
    for block in result["code_blocks"]:
        assert block["start_line"] >= 1, "start_line must be 1-based"
        assert (
            block["end_line"] >= block["start_line"]
        ), f"end_line ({block['end_line']}) < start_line ({block['start_line']})"
        assert block["symbol"], "symbol must not be empty"
        assert block["symbol_type"], "symbol_type must not be empty"
        assert block["change_type"] in {"modified", "added", "deleted"}
        assert block["file_path"], "file_path must not be empty"

    # symbols_modified/added/deleted should be consistent with code_blocks.
    total_symbols = (
        len(result["symbols_modified"])
        + len(result["symbols_added"])
        + len(result["symbols_deleted"])
    )
    assert total_symbols == len(result["code_blocks"])


@pytest.mark.integration_serial
class TestAnalyzeGoInstance:
    """Full pipeline test on a real Go instance from caddyserver/caddy."""

    def test_go_instance_produces_code_blocks(self, gt_locator, go_instance):
        result = gt_locator.analyze_instance(go_instance)
        _assert_valid_result(result)

        # Verify instance metadata is preserved.
        assert result["instance_id"] == go_instance["instance_id"]
        assert result["repo"] == go_instance["repo"]
        assert result["base_commit"] == go_instance["base_commit"]


@pytest.mark.integration_serial
class TestAnalyzeCppInstance:
    """Full pipeline test on a real C/C++ instance from redis/redis."""

    def test_cpp_instance_produces_code_blocks(self, gt_locator, cpp_instance):
        result = gt_locator.analyze_instance(cpp_instance)
        _assert_valid_result(result)

        assert result["instance_id"] == cpp_instance["instance_id"]
        assert result["repo"] == cpp_instance["repo"]


@pytest.mark.integration_serial
class TestAnalyzeRustInstance:
    """Full pipeline test on a real Rust instance from tokio-rs/axum."""

    def test_rust_instance_produces_code_blocks(self, gt_locator, rust_instance):
        result = gt_locator.analyze_instance(rust_instance)
        _assert_valid_result(result)

        assert result["instance_id"] == rust_instance["instance_id"]


@pytest.mark.integration_serial
class TestAnalyzeTypescriptInstance:
    """Full pipeline test on a real TypeScript instance from preactjs/preact."""

    def test_ts_instance_produces_code_blocks(self, gt_locator, typescript_instance):
        result = gt_locator.analyze_instance(typescript_instance)
        _assert_valid_result(result)

        assert result["instance_id"] == typescript_instance["instance_id"]
