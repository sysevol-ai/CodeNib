# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``scripts/profiling/profile_incremental_graph.py``.

The benchmark itself needs real SCIP/clangd + cloned repos (that side is an
integration concern, exercised manually). These tests cover the pure helpers
and the per-step comparison harness with mocked strategy sub-records — no LSP,
no git, no ``codenib.*`` heavy deps (those imports are lazy in the script).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "profiling" / "profile_incremental_graph.py"
_SPEC = importlib.util.spec_from_file_location(
    "_profile_incremental_graph_for_tests", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
prof = importlib.util.module_from_spec(_SPEC)
sys.modules["_profile_incremental_graph_for_tests"] = prof
_SPEC.loader.exec_module(prof)


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
    def test_excludes(self, path):
        assert prof.is_test_path(path) is True

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
    def test_includes(self, path):
        assert prof.is_test_path(path) is False


class TestParseInstancesInline:
    """Comma-separated instance spec parsing."""

    def test_three_field_form(self, tmp_path):
        out = prof.parse_instances_inline("foo:python:abc123,bar:go:def456", tmp_path)
        assert out == [("foo", "python", "abc123"), ("bar", "go", "def456")]

    def test_whitespace_tolerance(self, tmp_path):
        out = prof.parse_instances_inline(" foo:python:abc , bar:go:def ", tmp_path)
        assert out == [("foo", "python", "abc"), ("bar", "go", "def")]

    def test_skip_malformed(self, tmp_path, capsys):
        out = prof.parse_instances_inline("only_one_field,foo:python:abc", tmp_path)
        assert out == [("foo", "python", "abc")]
        assert "cannot parse" in capsys.readouterr().err

    def test_two_field_with_inference_failure(self, tmp_path, capsys):
        # No repo on disk -> inference returns None -> entry dropped + warning.
        out = prof.parse_instances_inline("ghost:abc123", tmp_path)
        assert out == []
        assert "language inference failed" in capsys.readouterr().err

    def test_two_field_with_inference_success(self, tmp_path):
        # Create a repo dir with .py files to force python inference.
        repo = tmp_path / "real" / "repo"
        repo.mkdir(parents=True)
        for i in range(3):
            (repo / f"f{i}.py").write_text("x = 1\n")
        out = prof.parse_instances_inline("real:abc123", tmp_path)
        assert out == [("real", "python", "abc123")]


class TestParseInstancesConfig:
    """JSON config file parsing."""

    def test_roundtrip(self, tmp_path):
        cfg = tmp_path / "instances.json"
        cfg.write_text(
            '[{"instance_id": "foo", "language": "rust", "base_commit": "aa"}]'
        )
        assert prof.parse_instances_config(cfg) == [("foo", "rust", "aa")]


class TestInferLanguage:
    """Dominant-language detection from a repo's file extensions."""

    def test_picks_dominant(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        for i in range(5):
            (repo / f"a{i}.rs").write_text("fn main() {}\n")
        (repo / "one.py").write_text("x = 1\n")
        assert prof._infer_language(repo) == "rust"

    def test_none_when_no_source(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("# hi\n")
        assert prof._infer_language(repo) is None

    def test_missing_dir(self, tmp_path):
        assert prof._infer_language(tmp_path / "nope") is None


class TestSummarizeLspProfile:
    """LSP profile JSONL -> aggregate summary."""

    def test_missing_file(self, tmp_path):
        out = prof._summarize_lsp_profile(tmp_path / "nope.jsonl")
        assert out == {"calls_total": 0, "calls_by_method": {}, "ms_by_method": {}}

    def test_none_path(self):
        out = prof._summarize_lsp_profile(None)
        assert out == {"calls_total": 0, "calls_by_method": {}, "ms_by_method": {}}

    def test_aggregates_by_method(self, tmp_path):
        p = tmp_path / "calls.jsonl"
        p.write_text(
            "\n".join(
                [
                    '{"m": "definition", "ms": 10.5, "n": 2}',
                    '{"m": "definition", "ms": 4.5, "n": 0}',
                    '{"m": "references", "ms": 100, "n": 5}',
                    "",
                    "garbage line - must not crash the parser",
                ]
            )
        )
        out = prof._summarize_lsp_profile(p)
        assert out["calls_total"] == 3
        assert out["calls_by_method"] == {"definition": 2, "references": 1}
        assert out["ms_by_method"]["definition"] == 15.0
        assert out["ms_by_method"]["references"] == 100.0
        assert out["empty_by_method"]["definition"] == 1
        assert out["empty_by_method"]["references"] == 0


class TestFirstSymbolPosition:
    """DFS into LSP documentSymbol output."""

    def test_empty(self):
        assert prof._first_symbol_position([]) is None
        assert prof._first_symbol_position(None) is None

    def test_top_level(self):
        syms = [{"selectionRange": {"start": {"line": 7, "character": 4}}}]
        assert prof._first_symbol_position(syms) == (7, 4)

    def test_falls_back_to_range(self):
        syms = [{"range": {"start": {"line": 12, "character": 0}}}]
        assert prof._first_symbol_position(syms) == (12, 0)

    def test_descends_into_children(self):
        syms = [
            {"children": []},  # no positional info
            {"children": [{"selectionRange": {"start": {"line": 99}}}]},
        ]
        assert prof._first_symbol_position(syms) == (99, 0)

    def test_skips_missing_line(self):
        syms = [
            {"range": {"start": {}}},  # missing line
            {"range": {"start": {"line": 3}}},
        ]
        assert prof._first_symbol_position(syms) == (3, 0)


class TestFinalizeStepRecord:
    """Per-step comparison harness: derive speedups from mocked strategy
    sub-records (no LSP / git / indexer)."""

    def _rec(self, rebuild=None, file_level=None, symbol=None):
        rec = {"instance_id": "x", "step": 1}
        if rebuild is not None:
            rec["fully-rebuild"] = rebuild
        if file_level is not None:
            rec["file-level-patch"] = file_level
        if symbol is not None:
            rec["symbol-level-patch"] = symbol
        return rec

    def test_speedups_computed(self):
        rec = self._rec(
            rebuild={"status": "ok", "t_s": 100.0},
            file_level={"status": "ok", "t_s": 40.0},
            symbol={"status": "ok", "t_s": 10.0},
        )
        out = prof.finalize_step_record(rec)
        assert out["speedup_vs_fully_rebuild"] == pytest.approx(10.0)
        assert out["speedup_vs_file_level_patch"] == pytest.approx(4.0)
        assert "finished" in out

    def test_none_when_symbol_failed(self):
        # Symbol strategy errored -> no t_s denominator -> ratios are None.
        rec = self._rec(
            rebuild={"status": "ok", "t_s": 100.0},
            file_level={"status": "ok", "t_s": 40.0},
            symbol={"status": "error", "error": "boom"},
        )
        out = prof.finalize_step_record(rec)
        assert out["speedup_vs_fully_rebuild"] is None
        assert out["speedup_vs_file_level_patch"] is None

    def test_none_when_rebuild_failed(self):
        # Rebuild timed out -> rebuild ratio None, but file-level ratio holds.
        rec = self._rec(
            rebuild={"status": "timeout", "t_s": 600.0},
            file_level={"status": "ok", "t_s": 40.0},
            symbol={"status": "ok", "t_s": 10.0},
        )
        out = prof.finalize_step_record(rec)
        assert out["speedup_vs_fully_rebuild"] is None
        assert out["speedup_vs_file_level_patch"] == pytest.approx(4.0)

    def test_missing_strategies_are_safe(self):
        # No strategy sub-records at all -> both ratios None, no crash.
        out = prof.finalize_step_record(self._rec())
        assert out["speedup_vs_fully_rebuild"] is None
        assert out["speedup_vs_file_level_patch"] is None


class TestStrategyNames:
    """Guard the three strategy labels the issue specifies stay wired up."""

    def test_default_instances_cover_five_languages(self):
        langs = {lang for (_iid, lang, _sha) in prof.DEFAULT_INSTANCES}
        assert langs == {"cpp", "go", "python", "rust", "ts"}

    def test_lang_exts_and_pathspec_cover_same_languages(self):
        assert set(prof.LANG_EXTS) == set(prof.LANG_PATHSPEC)
