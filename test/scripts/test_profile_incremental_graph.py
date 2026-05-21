#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pure helpers in scripts/profile_incremental_graph.py.

The script transitively imports the full ``codeminer`` package (which pulls
litellm and other heavy deps at import time). Tests here stub those imports
via ``sys.modules`` so the pure helpers can be loaded without a full dev
environment.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "profile_incremental_graph.py"
)


def _load_script_module():
    """Load the benchmark script with codeminer.* deps stubbed."""
    cached = sys.modules.get("profile_incremental_graph")
    if cached is not None:
        return cached

    stubs = {
        "codeminer": types.ModuleType("codeminer"),
        "codeminer.code_chunker": types.ModuleType("codeminer.code_chunker"),
        "codeminer.graph": types.ModuleType("codeminer.graph"),
        "codeminer.graph.code_graph": types.ModuleType("codeminer.graph.code_graph"),
        "codeminer.graph.incremental": types.ModuleType("codeminer.graph.incremental"),
        "codeminer.graph.incremental.graph_patcher": types.ModuleType(
            "codeminer.graph.incremental.graph_patcher"
        ),
        "codeminer.ls_router": types.ModuleType("codeminer.ls_router"),
    }
    stubs["codeminer.code_chunker"].CodeChunker = type("CodeChunker", (), {})
    stubs["codeminer.graph.code_graph"].CodeGraph = type("CodeGraph", (), {})
    stubs["codeminer.graph.incremental.graph_patcher"].GraphPatcher = type(
        "GraphPatcher", (), {}
    )
    stubs["codeminer.ls_router"].LSIndexer = type("LSIndexer", (), {})

    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "profile_incremental_graph", _SCRIPT_PATH
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["profile_incremental_graph"] = module
        spec.loader.exec_module(module)
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script_module()


class TestIsTestPath:
    """Files/dirs the test-exclusion filter should catch vs. let through."""

    @pytest.mark.parametrize(
        "path",
        [
            "test/foo.py",
            "tests/bar.go",
            "src/__tests__/baz.ts",
            "pkg/testing/x.go",
            "fixtures/data.json",
            "testdata/sample.txt",
            "src/test_helper.py",
            "src/handler_test.go",
            "src/handler_test.py",
            "src/component.test.ts",
            "src/component.test.tsx",
            "src/component.spec.js",
            "src/SPEC.spec.ts",
            "deeply/nested/specs/x.py",
        ],
    )
    def test_excludes(self, script, path):
        assert script.is_test_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/foo.py",
            "lib/handler.go",
            "pkg/server/main.go",
            "src/component.ts",
            "src/myspec.py",
            "src/contestant.py",
        ],
    )
    def test_includes(self, script, path):
        assert script.is_test_path(path) is False


class TestParseInstancesInline:
    """Comma-separated instance spec parsing."""

    def test_three_field_form(self, script, tmp_path):
        out = script.parse_instances_inline("foo:python:abc123,bar:go:def456", tmp_path)
        assert out == [("foo", "python", "abc123"), ("bar", "go", "def456")]

    def test_whitespace_tolerance(self, script, tmp_path):
        out = script.parse_instances_inline(" foo:python:abc , bar:go:def ", tmp_path)
        assert out == [("foo", "python", "abc"), ("bar", "go", "def")]

    def test_skip_malformed(self, script, tmp_path, capsys):
        out = script.parse_instances_inline("only_one_field,foo:python:abc", tmp_path)
        assert out == [("foo", "python", "abc")]
        assert "cannot parse" in capsys.readouterr().err

    def test_two_field_with_inference_failure(self, script, tmp_path, capsys):
        # No repo on disk → inference returns None → entry dropped with warning.
        out = script.parse_instances_inline("ghost:abc123", tmp_path)
        assert out == []
        assert "language inference failed" in capsys.readouterr().err

    def test_two_field_with_inference_success(self, script, tmp_path):
        # Create a repo dir with .py files to force python inference.
        repo = tmp_path / "real" / "repo"
        repo.mkdir(parents=True)
        for i in range(3):
            (repo / f"f{i}.py").write_text("x = 1\n")
        out = script.parse_instances_inline("real:abc123", tmp_path)
        assert out == [("real", "python", "abc123")]


class TestSummarizeLspProfile:
    """LSP profile JSONL → aggregate summary."""

    def test_missing_file(self, script, tmp_path):
        out = script._summarize_lsp_profile(tmp_path / "nope.jsonl")
        assert out == {"calls_total": 0, "calls_by_method": {}, "ms_by_method": {}}

    def test_aggregates_by_method(self, script, tmp_path):
        p = tmp_path / "calls.jsonl"
        p.write_text(
            "\n".join(
                [
                    '{"m": "definition", "ms": 10.5, "n": 2}',
                    '{"m": "definition", "ms": 4.5, "n": 0}',
                    '{"m": "references", "ms": 100, "n": 5}',
                    "",
                    "garbage line — must not crash the parser",
                ]
            )
        )
        out = script._summarize_lsp_profile(p)
        assert out["calls_total"] == 3
        assert out["calls_by_method"] == {"definition": 2, "references": 1}
        assert out["ms_by_method"]["definition"] == 15.0
        assert out["ms_by_method"]["references"] == 100.0
        assert out["empty_by_method"]["definition"] == 1
        assert out["empty_by_method"]["references"] == 0


class TestFirstSymbolPosition:
    """DFS into LSP documentSymbol output."""

    def test_empty(self, script):
        assert script._first_symbol_position([]) is None
        assert script._first_symbol_position(None) is None

    def test_top_level(self, script):
        syms = [{"selectionRange": {"start": {"line": 7, "character": 4}}}]
        assert script._first_symbol_position(syms) == (7, 4)

    def test_falls_back_to_range(self, script):
        syms = [{"range": {"start": {"line": 12, "character": 0}}}]
        assert script._first_symbol_position(syms) == (12, 0)

    def test_descends_into_children(self, script):
        syms = [
            {"children": []},  # no positional info
            {"children": [{"selectionRange": {"start": {"line": 99}}}]},
        ]
        assert script._first_symbol_position(syms) == (99, 0)

    def test_skips_missing_line(self, script):
        syms = [
            {"range": {"start": {}}},  # missing line
            {"range": {"start": {"line": 3}}},
        ]
        assert script._first_symbol_position(syms) == (3, 0)
