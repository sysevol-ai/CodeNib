# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end smoke tests for the compile_table runtime wiring (issue #149).

These tests exercise the full path: classify(query) → compile_table lookup
→ AgentRunner.tool narrowing → registry_to_tools. The LLM is mocked so no
network calls happen; the tests only verify that the *set of tools the
LLM sees* corresponds to the compile-table-resolved subset.
"""

from __future__ import annotations

import json
import logging
from typing import List
from unittest.mock import MagicMock

import pytest

from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.compiler.params import SessionContext
from codeminer.llm.litellm_chat import LiteLLMChat


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


@pytest.fixture()
def skills() -> SkillRegistry:
    reg = SkillRegistry()
    for sid in (
        "bm25_search",
        "embedding_search",
        "graph_expand",
        "regex_search",
    ):
        reg.register(
            SkillMetadata(
                skill_id=sid,
                skill_type=SkillType.RETRIEVAL,
                inputs=[SkillInputSpec(name="q", type_hint="str", required=True)],
                outputs=SkillOutputSpec(type_hint="str"),
                executor_fn=lambda q, _sid=sid: f"{_sid}: {q}",
            )
        )
    return reg


def _mock_llm_no_tool_call() -> LiteLLMChat:
    """An LLM that immediately produces a final answer (no tool call)."""
    llm = MagicMock(spec=LiteLLMChat)
    assistant = MagicMock()
    assistant.content = "done"
    assistant.tool_calls = None
    assistant.model_dump = MagicMock(
        return_value={"role": "assistant", "content": "done"}
    )
    choice = MagicMock()
    choice.message = assistant
    response = MagicMock()
    response.choices = [choice]
    llm._call_raw = MagicMock(return_value=response)
    return llm


def _tools_passed_to_llm(llm) -> List[str]:
    """Pull the tools kwarg from the last ``_call_raw`` invocation."""
    args, kwargs = llm._call_raw.call_args
    schemas = kwargs.get("tools", [])
    return sorted(t["function"]["name"] for t in schemas)


# ---------------------------------------------------------------------------
# Table narrows the allow set
# ---------------------------------------------------------------------------


class TestCompileTableNarrowing:
    def test_stacktrace_scenario_collapses_to_bm25(self, skills):
        """A query with a Python traceback hits ``python:stacktrace`` → A0."""
        table = {
            "python:stacktrace": frozenset({"bm25_search"}),
            "python:no_stacktrace": frozenset(
                {"bm25_search", "embedding_search", "graph_expand"}
            ),
        }
        llm = _mock_llm_no_tool_call()
        runner = AgentRunner(
            llm=llm,
            registry=skills,
            session_ctx=SessionContext(primary_language="python"),
            compile_table=table,
        )
        query = (
            "Test fails:\nTraceback (most recent call last):\n"
            "  File 'x.py', line 1, in foo\n    raise ValueError(x)\n"
        )
        runner.run(query)
        assert _tools_passed_to_llm(llm) == ["bm25_search"]

    def test_no_stacktrace_scenario_uses_wider_subset(self, skills):
        table = {
            "python:stacktrace": frozenset({"bm25_search"}),
            "python:no_stacktrace": frozenset(
                {"bm25_search", "embedding_search", "graph_expand"}
            ),
        }
        llm = _mock_llm_no_tool_call()
        runner = AgentRunner(
            llm=llm,
            registry=skills,
            session_ctx=SessionContext(primary_language="python"),
            compile_table=table,
        )
        runner.run("Refactor the parser to support new syntax.")
        assert _tools_passed_to_llm(llm) == [
            "bm25_search",
            "embedding_search",
            "graph_expand",
        ]

    def test_table_intersects_with_user_allow(self, skills):
        """User's ``allow_skills`` is the upper bound — table can only narrow."""
        table = {
            "python:no_stacktrace": frozenset(
                {"bm25_search", "embedding_search", "graph_expand"}
            ),
        }
        llm = _mock_llm_no_tool_call()
        runner = AgentRunner(
            llm=llm,
            registry=skills,
            session_ctx=SessionContext(primary_language="python"),
            compile_table=table,
            allow_skills={"bm25_search", "embedding_search"},
        )
        runner.run("Some refactor query")
        # Intersection of table {bm25, embedding, graph} with allow
        # {bm25, embedding} = {bm25, embedding}. graph_expand is gone.
        assert _tools_passed_to_llm(llm) == ["bm25_search", "embedding_search"]


# ---------------------------------------------------------------------------
# Empty-allow fallback (issue #149 contract)
# ---------------------------------------------------------------------------


class TestEmptyAllowFallback:
    def test_table_miss_uses_full_registry(self, skills):
        """Scenario not in the table → agent_compile returns None →
        runner keeps the constructor-time allow (None) → full registry."""
        table = {"python:stacktrace": frozenset({"bm25_search"})}
        llm = _mock_llm_no_tool_call()
        runner = AgentRunner(
            llm=llm,
            registry=skills,
            session_ctx=SessionContext(primary_language="rust"),
            compile_table=table,
        )
        runner.run("Refactor the rust parser.")
        # rust:no_stacktrace not in table → full registry
        assert _tools_passed_to_llm(llm) == sorted(
            ["bm25_search", "embedding_search", "graph_expand", "regex_search"]
        )

    def test_table_returns_empty_subset_falls_back_to_full(self, skills):
        """A table entry mapping a scenario to an empty subset means the
        designer accidentally pruned everything; the runner falls back
        to the full registry (loudly) rather than stalling silently.
        """
        table = {"python:stacktrace": frozenset()}
        llm = _mock_llm_no_tool_call()

        captured: List[logging.LogRecord] = []
        runner_logger = logging.getLogger("codeminer.agent.runner")

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Handler(level=logging.WARNING)
        runner_logger.addHandler(handler)
        try:
            runner = AgentRunner(
                llm=llm,
                registry=skills,
                session_ctx=SessionContext(primary_language="python"),
                compile_table=table,
            )
            runner.run("Traceback (most recent call last):\n  File 'x.py'")
        finally:
            runner_logger.removeHandler(handler)

        names = _tools_passed_to_llm(llm)
        assert set(names) == {
            "bm25_search",
            "embedding_search",
            "graph_expand",
            "regex_search",
        }
        assert any("empty allow_skills" in r.getMessage() for r in captured)

    def test_disjoint_table_and_allow_keeps_allow_not_full_registry(self, skills):
        """Regression: a table subset disjoint from a non-empty
        ``allow_skills`` upper bound must fall back to the upper bound,
        NOT broaden back to the full registry via the empty→full path.

        Before the fix, ``{embedding_search} & {bm25_search} == set()``
        hit the empty-allowlist contract and exposed all four skills,
        silently violating the funnel invariant
        ``registry ⊇ allow_skills ⊇ table[scenario]``.
        """
        table = {"python:stacktrace": frozenset({"embedding_search"})}
        llm = _mock_llm_no_tool_call()

        captured: List[logging.LogRecord] = []
        runner_logger = logging.getLogger("codeminer.agent.runner")

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Handler(level=logging.WARNING)
        runner_logger.addHandler(handler)
        try:
            runner = AgentRunner(
                llm=llm,
                registry=skills,
                session_ctx=SessionContext(primary_language="python"),
                compile_table=table,
                allow_skills={"bm25_search"},
            )
            runner.run("Traceback (most recent call last):\n  File 'x.py'")
        finally:
            runner_logger.removeHandler(handler)

        # Falls back to the allow_skills upper bound, never the full registry.
        assert _tools_passed_to_llm(llm) == ["bm25_search"]
        assert any("disjoint" in r.getMessage() for r in captured)


# ---------------------------------------------------------------------------
# YAML / JSON loader roundtrip via AgentRunner
# ---------------------------------------------------------------------------


class TestLoaderToRunner:
    def test_json_file_drives_narrowing(self, skills, tmp_path):
        """Load a compile_table from disk (JSON) and feed it to the runner."""
        from codeminer.agent.compile import load_compile_table

        path = tmp_path / "compile_table.json"
        path.write_text(
            json.dumps(
                {
                    "python:stacktrace": ["bm25_search"],
                    "python:no_stacktrace": [
                        "bm25_search",
                        "embedding_search",
                    ],
                }
            )
        )
        table = load_compile_table(path)
        llm = _mock_llm_no_tool_call()
        runner = AgentRunner(
            llm=llm,
            registry=skills,
            session_ctx=SessionContext(primary_language="python"),
            compile_table=table,
        )
        runner.run("Need to add a feature.")
        assert _tools_passed_to_llm(llm) == ["bm25_search", "embedding_search"]

    def test_yaml_file_drives_narrowing(self, skills, tmp_path):
        from codeminer.agent.compile import load_compile_table

        path = tmp_path / "compile_table.yaml"
        path.write_text(
            "python:stacktrace: [bm25_search]\n"
            "python:no_stacktrace:\n"
            "  - bm25_search\n"
            "  - graph_expand\n"
        )
        table = load_compile_table(path)
        llm = _mock_llm_no_tool_call()
        runner = AgentRunner(
            llm=llm,
            registry=skills,
            session_ctx=SessionContext(primary_language="python"),
            compile_table=table,
        )
        runner.run("Refactor the parser.")
        assert _tools_passed_to_llm(llm) == ["bm25_search", "graph_expand"]
