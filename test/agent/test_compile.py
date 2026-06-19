# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``codeminer.agent.compile`` (issue #133, Phase 1)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from codeminer.agent.compile import (
    SUPPORTED_LANGUAGES,
    Scenario,
    agent_compile,
    classify,
    detect_stacktrace,
    load_compile_table,
    normalize_language,
)
from codeminer.compiler.params import SessionContext

# ---------------------------------------------------------------------------
# detect_stacktrace
# ---------------------------------------------------------------------------


class TestDetectStacktrace:
    """Each entry pairs a runtime with a representative trace snippet."""

    def test_python_traceback(self):
        query = (
            "Test fails with:\n"
            "Traceback (most recent call last):\n"
            '  File "foo.py", line 12, in bar\n'
            "    raise ValueError(x)\n"
            "ValueError: bad\n"
        )
        assert detect_stacktrace(query) is True

    def test_rust_panic(self):
        query = (
            "thread 'main' panicked at 'index out of bounds: the len is 3 "
            "but the index is 5', src/lib.rs:42:9\n"
        )
        assert detect_stacktrace(query) is True

    def test_go_panic(self):
        query = (
            "panic: runtime error: invalid memory address\n"
            "\n"
            "goroutine 1 [running]:\n"
            "main.main()\n"
            "\t/app/main.go:18 +0x39\n"
        )
        assert detect_stacktrace(query) is True

    def test_node_stack_frame(self):
        query = (
            "TypeError: cannot read properties of null\n"
            "    at process (/app/index.js:42:18)\n"
            "    at Object.<anonymous> (/app/index.js:88:3)\n"
        )
        assert detect_stacktrace(query) is True

    def test_jvm_stack_frame(self):
        query = (
            'Exception in thread "main" java.lang.NullPointerException\n'
            "\tat com.example.Foo.bar(Foo.java:42)\n"
            "\tat com.example.Main.main(Main.java:8)\n"
        )
        assert detect_stacktrace(query) is True

    def test_kotlin_jvm_stack_frame(self):
        query = (
            'Exception in thread "main" java.lang.IllegalStateException\n'
            "\tat com.example.Point.move(Point.kt:42)\n"
            "\tat com.example.MainKt.main(Main.kt:8)\n"
        )
        assert detect_stacktrace(query) is True

    def test_dotnet_stack_frame(self):
        query = (
            "System.NullReferenceException: Object reference not set\n"
            "   at Billing.Invoice.Total() in /repo/src/Invoice.cs:line 42\n"
            "   at Billing.Program.Main() in /repo/src/Program.cs:line 8\n"
        )
        assert detect_stacktrace(query) is True

    def test_php_stack_frame(self):
        query = (
            "Fatal error: Uncaught Error: Call to a member function run() on null\n"
            "#0 /app/src/Invoice.php(42): Billing\\Invoice->total()\n"
            "#1 /app/index.php(8): main()\n"
        )
        assert detect_stacktrace(query) is True

    def test_ruby_stack_frame(self):
        query = (
            "NoMethodError: undefined method `name' for nil:NilClass\n"
            "/app/lib/billing/invoice.rb:42:in `total'\n"
            "\tfrom /app/bin/app.rb:8:in '<main>'\n"
        )
        assert detect_stacktrace(query) is True

    def test_c_backtrace(self):
        query = (
            "Program received signal SIGSEGV\n"
            "#0  0x00007fff in foo at /repo/src/foo.c:42\n"
            "#1  0x00008000 in main at /repo/src/main.c:18\n"
        )
        assert detect_stacktrace(query) is True

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "I get a traceback when I run the tests, please help.",
            "The function may panic if the input is invalid.",
            "Reading the panic documentation didn't help.",
            "We are looking at a regression around line 42 of foo.py.",
        ],
    )
    def test_prose_does_not_match(self, query):
        """User prose mentioning trace-like words must not flip the flag.

        The has_stacktrace dimension is only meaningful if it picks up
        *structural* trace markers; otherwise the Scenario classifier output
        is polluted with false positives.
        """
        assert detect_stacktrace(query) is False


# ---------------------------------------------------------------------------
# normalize_language
# ---------------------------------------------------------------------------


class TestNormalizeLanguage:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Python", "python"),
            ("py", "python"),
            ("python3", "python"),
            ("Go", "go"),
            ("golang", "go"),
            ("Rust", "rust"),
            ("rs", "rust"),
            ("C++", "cpp"),
            ("cxx", "cpp"),
            ("C", "c"),
            ("C#", "csharp"),
            ("cs", "csharp"),
            ("Java", "java"),
            ("Ruby", "ruby"),
            ("rb", "ruby"),
            ("PHP", "php"),
            ("Kotlin", "kotlin"),
            ("kt", "kotlin"),
            ("TypeScript", "typescript"),
            ("ts", "typescript"),
            ("JavaScript", "javascript"),
            ("js", "javascript"),
            ("jsx", "javascript"),
            ("tsx", "typescript"),
        ],
    )
    def test_canonical_mapping(self, raw, expected):
        assert normalize_language(raw) == expected
        assert expected in SUPPORTED_LANGUAGES

    @pytest.mark.parametrize("raw", [None, "", "  ", "haskell", "ocaml", "fortran"])
    def test_unknown_or_empty(self, raw):
        assert normalize_language(raw) is None


# ---------------------------------------------------------------------------
# Scenario / classify
# ---------------------------------------------------------------------------


class TestScenario:
    def test_key_roundtrip_concrete(self):
        s = Scenario(language="python", has_stacktrace=True)
        assert s.key() == "python:stacktrace"
        assert Scenario.from_key(s.key()) == s

    def test_key_roundtrip_no_stacktrace(self):
        s = Scenario(language="rust", has_stacktrace=False)
        assert s.key() == "rust:no_stacktrace"
        assert Scenario.from_key(s.key()) == s

    def test_key_roundtrip_unknown_language(self):
        s = Scenario(language=None, has_stacktrace=True)
        assert s.key() == "unknown:stacktrace"
        assert Scenario.from_key(s.key()) == s

    def test_scenario_hashable(self):
        """Frozen dataclass with slots — usable as a dict key."""
        s1 = Scenario(language="python", has_stacktrace=True)
        s2 = Scenario(language="python", has_stacktrace=True)
        d = {s1: "A0"}
        assert d[s2] == "A0"


class TestClassify:
    def test_python_with_traceback(self):
        ctx = SessionContext(primary_language="python")
        s = classify("Traceback (most recent call last):\n  File 'x.py'", ctx)
        assert s == Scenario(language="python", has_stacktrace=True)

    def test_rust_without_trace(self):
        ctx = SessionContext(primary_language="Rust")
        s = classify("Refactor the parser to support new syntax.", ctx)
        assert s == Scenario(language="rust", has_stacktrace=False)

    def test_none_session_falls_back_to_unknown_language(self):
        s = classify("Test failure", None)
        assert s == Scenario(language=None, has_stacktrace=False)

    def test_unknown_language_normalizes_to_none(self):
        ctx = SessionContext(primary_language="haskell")
        s = classify("oops", ctx)
        assert s.language is None


# ---------------------------------------------------------------------------
# load_compile_table
# ---------------------------------------------------------------------------


class TestLoadCompileTable:
    def test_roundtrip(self, tmp_path: Path):
        payload = {
            "python:stacktrace": ["bm25_search"],
            "python:no_stacktrace": [
                "bm25_search",
                "embedding_search",
                "find_callers",
            ],
        }
        p = tmp_path / "compile_table.json"
        p.write_text(json.dumps(payload))
        table = load_compile_table(p)
        assert table["python:stacktrace"] == frozenset({"bm25_search"})
        assert table["python:no_stacktrace"] == frozenset(
            {"bm25_search", "embedding_search", "find_callers"}
        )

    def test_rejects_non_object(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(["bm25_search"]))
        with pytest.raises(ValueError, match="must be a mapping"):
            load_compile_table(p)

    def test_rejects_non_list_value(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"python:stacktrace": "bm25_search"}))
        with pytest.raises(ValueError, match="must be a list"):
            load_compile_table(p)

    def test_yaml_roundtrip(self, tmp_path: Path):
        """Sub-issue #149 requires both JSON and YAML on disk."""
        p = tmp_path / "compile_table.yaml"
        p.write_text(
            "python:stacktrace:\n"
            "  - bm25_search\n"
            "python:no_stacktrace:\n"
            "  - bm25_search\n"
            "  - embedding_search\n"
            "  - find_callers\n"
        )
        table = load_compile_table(p)
        assert table["python:stacktrace"] == frozenset({"bm25_search"})
        assert table["python:no_stacktrace"] == frozenset(
            {"bm25_search", "embedding_search", "find_callers"}
        )

    def test_yml_extension_also_works(self, tmp_path: Path):
        p = tmp_path / "compile_table.yml"
        p.write_text("rust:stacktrace: [bm25_search]\n")
        table = load_compile_table(p)
        assert table["rust:stacktrace"] == frozenset({"bm25_search"})

    def test_rejects_unknown_extension(self, tmp_path: Path):
        p = tmp_path / "compile_table.txt"
        p.write_text("doesn't matter")
        with pytest.raises(ValueError, match="unsupported extension"):
            load_compile_table(p)


# ---------------------------------------------------------------------------
# agent_compile
# ---------------------------------------------------------------------------


class TestAgentCompile:
    def test_table_missing_returns_none(self):
        """No table → no restriction → caller exposes full registry (A6)."""
        ctx = SessionContext(primary_language="python")
        assert agent_compile("any query", ctx, None) is None
        assert agent_compile("any query", ctx, {}) is None

    def test_scenario_hit(self):
        table = {
            "python:stacktrace": ["bm25_search"],
            "python:no_stacktrace": ["bm25_search", "embedding_search"],
        }
        ctx = SessionContext(primary_language="python")
        trace_query = "Traceback (most recent call last):\n  File 'x.py'"
        skills = agent_compile(trace_query, ctx, table)
        assert skills == frozenset({"bm25_search"})

    def test_scenario_miss_falls_back_to_none(self, caplog):
        """Unknown scenario → None (= A6) + WARN log."""
        table = {"python:stacktrace": ["bm25_search"]}
        ctx = SessionContext(primary_language="rust")
        with caplog.at_level(logging.WARNING):
            skills = agent_compile("refactor the parser", ctx, table)
        assert skills is None
        assert any("rust:no_stacktrace" in r.message for r in caplog.records)

    def test_none_session_ctx(self):
        """Missing session ctx → language=None scenario key."""
        table = {"unknown:no_stacktrace": ["bm25_search", "embedding_search"]}
        skills = agent_compile("find the foo function", None, table)
        assert skills == frozenset({"bm25_search", "embedding_search"})
