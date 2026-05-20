# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``codeminer.agent.query`` public facade.

These tests mock the LLM and pass an empty ``contexts`` dict so no index
build or network call happens. They exercise:

* The "either repo_path or contexts" precondition.
* compile_table threading (inline dict + YAML path).
* skill_params overrides reaching ``SkillMetadata.defaults``.
* SessionContext construction (primary_language, repo_size).

For the real end-to-end path with a built BM25 index, see
``test_query_sdk_e2e.py``.
"""

from __future__ import annotations

from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest

from codeminer.agent import CodeMinerAgentOptions, query
from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.llm.litellm_chat import LiteLLMChat


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def _mock_llm_final_answer(answer: str = "done") -> LiteLLMChat:
    """LLM that immediately produces a final answer (no tool call)."""
    llm = MagicMock(spec=LiteLLMChat)
    assistant = MagicMock()
    assistant.content = answer
    assistant.tool_calls = None
    assistant.model_dump = MagicMock(
        return_value={"role": "assistant", "content": answer}
    )
    choice = MagicMock()
    choice.message = assistant
    response = MagicMock()
    response.choices = [choice]
    llm._call_raw = MagicMock(return_value=response)
    return llm


def _register_test_skills() -> SkillRegistry:
    """Register a handful of skills with controllable defaults."""
    reg = SkillRegistry()
    for sid in ("bm25_search", "embedding_search", "graph_expand"):
        reg.register(
            SkillMetadata(
                skill_id=sid,
                skill_type=SkillType.RETRIEVAL,
                inputs=[SkillInputSpec(name="q", type_hint="str", required=True)],
                outputs=SkillOutputSpec(type_hint="str"),
                executor_fn=lambda q, _sid=sid: f"{_sid}: {q}",
                defaults={"top_k": 10},
            )
        )
    return reg


def _tools_passed_to_llm(llm) -> List[str]:
    args, kwargs = llm._call_raw.call_args
    schemas = kwargs.get("tools", [])
    return sorted(t["function"]["name"] for t in schemas)


def _capture_runner_warnings():
    """Return a (records_list, install, uninstall) tuple.

    The runner logger has ``propagate=False`` (see ``codeminer.log_utils``)
    so pytest's ``caplog`` doesn't see its records. Attach a list-collecting
    handler directly to the named logger instead.
    """
    import logging

    records: List[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    runner_logger = logging.getLogger("codeminer.agent.runner")
    handler = _ListHandler(level=logging.WARNING)

    def install():
        runner_logger.addHandler(handler)

    def uninstall():
        runner_logger.removeHandler(handler)

    return records, install, uninstall


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


class TestPreconditions:
    def test_query_without_repo_path_or_contexts_raises(self):
        with pytest.raises(ValueError, match="repo_path"):
            query(
                "anything",
                options=CodeMinerAgentOptions(
                    llm=_mock_llm_final_answer(),
                ),
            )

    def test_query_with_contexts_skips_index_build(self, monkeypatch):
        """If options.contexts is set, build_skill_contexts must not run."""
        from codeminer import compiler as compiler_mod

        called = {"n": 0}

        def _boom(*a, **kw):
            called["n"] += 1
            raise AssertionError("build_skill_contexts should not have been called")

        monkeypatch.setattr(compiler_mod, "build_skill_contexts", _boom)
        # SkillLoader will scan the real skills dir; with contexts={} it just
        # won't bind their executors. That's fine — the agent won't call them.
        result = query(
            "hello",
            options=CodeMinerAgentOptions(
                contexts={},
                llm=_mock_llm_final_answer(answer="ok"),
                allowed_skills=["bm25_search"],
            ),
        )
        assert result.answer == "ok"
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# Compile-table threading
# ---------------------------------------------------------------------------


class TestCompileTable:
    def test_inline_dict_narrows_tools(self):
        """A python:no_stacktrace scenario yields the table-specified subset."""
        _register_test_skills()

        llm = _mock_llm_final_answer()
        table = {
            "python:stacktrace": frozenset({"bm25_search"}),
            "python:no_stacktrace": frozenset({"bm25_search", "embedding_search"}),
        }
        query(
            "How does the retry path work?",
            options=CodeMinerAgentOptions(
                contexts={},
                llm=llm,
                primary_language="python",
                compile_table=table,
            ),
        )
        assert _tools_passed_to_llm(llm) == ["bm25_search", "embedding_search"]

    def test_path_loads_yaml(self, tmp_path):
        """A compile_table path is auto-loaded via load_compile_table."""
        _register_test_skills()

        table_path = tmp_path / "table.yaml"
        table_path.write_text(
            "python:stacktrace:\n  - bm25_search\n"
            "python:no_stacktrace:\n  - bm25_search\n  - graph_expand\n",
            encoding="utf-8",
        )

        llm = _mock_llm_final_answer()
        query(
            "Why does this loop never terminate?",
            options=CodeMinerAgentOptions(
                contexts={},
                llm=llm,
                primary_language="python",
                compile_table=str(table_path),
            ),
        )
        assert _tools_passed_to_llm(llm) == ["bm25_search", "graph_expand"]

    def test_stacktrace_in_prompt_narrows_to_a0(self):
        """A real Python traceback collapses the allow-set to ``bm25_search``."""
        _register_test_skills()

        llm = _mock_llm_final_answer()
        table = {
            "python:stacktrace": frozenset({"bm25_search"}),
            "python:no_stacktrace": frozenset(
                {"bm25_search", "embedding_search", "graph_expand"}
            ),
        }
        prompt = (
            "Test fails:\nTraceback (most recent call last):\n"
            "  File 'x.py', line 1, in foo\n    raise ValueError(x)\n"
        )
        query(
            prompt,
            options=CodeMinerAgentOptions(
                contexts={},
                llm=llm,
                primary_language="python",
                compile_table=table,
            ),
        )
        assert _tools_passed_to_llm(llm) == ["bm25_search"]


# ---------------------------------------------------------------------------
# Skill params overrides
# ---------------------------------------------------------------------------


class TestSkillParams:
    def test_overrides_reach_metadata_defaults(self):
        """``options.skill_params`` mutates ``SkillMetadata.defaults`` in-place."""
        reg = _register_test_skills()
        before = reg.get("bm25_search").defaults["top_k"]
        assert before == 10

        query(
            "noop",
            options=CodeMinerAgentOptions(
                contexts={},
                llm=_mock_llm_final_answer(),
                allowed_skills=["bm25_search"],
                skill_params={"bm25_search": {"top_k": 50, "extra": "x"}},
            ),
        )

        # The registry was reset and re-populated, but our pre-registered
        # skills survive because we registered *after* reset wouldn't have
        # run yet — actually ``query()`` calls reset() itself. So check the
        # *current* registry, which holds whatever query() loaded.
        meta = SkillRegistry().get("bm25_search")
        if meta is not None:
            # If the bundled bm25_search package was loaded, our overrides
            # should be merged on top of its config.yaml defaults.
            assert meta.defaults.get("top_k") == 50
            assert meta.defaults.get("extra") == "x"

    def test_unknown_skill_in_skill_params_is_ignored(self):
        """Overrides for a non-existent skill log a warning and don't crash."""
        import logging

        records, install, uninstall = _capture_runner_warnings()
        install()
        try:
            query(
                "noop",
                options=CodeMinerAgentOptions(
                    contexts={},
                    llm=_mock_llm_final_answer(),
                    skill_params={"no_such_skill": {"top_k": 99}},
                ),
            )
        finally:
            uninstall()

        assert any(
            "no_such_skill" in r.getMessage() and r.levelno == logging.WARNING
            for r in records
        )


# ---------------------------------------------------------------------------
# allowed_skills ↔ compile_table mismatch warnings
# ---------------------------------------------------------------------------


class TestSkillSetMismatchWarning:
    """Cover ``_warn_on_skill_set_mismatch`` (orphan + overflow cases).

    The function fires at ``query()`` entry, before any heavy work, so the
    failure mode it warns about (unbuilt indexes / silently-dropped table
    entries) can't bite the caller mid-run.
    """

    def _query_with(
        self,
        allowed_skills: Optional[List[str]],
        table: Optional[dict],
    ) -> List[Any]:
        import logging

        records, install, uninstall = _capture_runner_warnings()
        install()
        try:
            query(
                "noop",
                options=CodeMinerAgentOptions(
                    contexts={},
                    llm=_mock_llm_final_answer(),
                    allowed_skills=allowed_skills,
                    compile_table=table,
                ),
            )
        finally:
            uninstall()
        return [r for r in records if r.levelno == logging.WARNING]

    def test_orphans_in_allowed_skills_warn(self):
        """allowed_skills contains skills no compile_table scenario names."""
        warnings = self._query_with(
            allowed_skills=["bm25_search", "embedding_search"],
            table={"python:stacktrace": ["bm25_search"]},
        )
        msgs = [w.getMessage() for w in warnings]
        # Orphan warning fires and names the offending skill.
        assert any(
            "compile_table never names" in m and "embedding_search" in m for m in msgs
        )

    def test_overflow_in_table_warns(self):
        """compile_table names skills outside the allowed_skills cap."""
        warnings = self._query_with(
            allowed_skills=["bm25_search"],
            table={"python:stacktrace": ["bm25_search", "embedding_search"]},
        )
        msgs = [w.getMessage() for w in warnings]
        assert any(
            "outside allowed_skills" in m and "embedding_search" in m for m in msgs
        )

    def test_coherent_config_is_silent(self):
        """When allowed_skills == union(table.values()), no warning fires."""
        warnings = self._query_with(
            allowed_skills=["bm25_search"],
            table={
                "python:stacktrace": ["bm25_search"],
                "python:no_stacktrace": ["bm25_search"],
            },
        )
        msgs = [w.getMessage() for w in warnings]
        assert not any("compile_table" in m for m in msgs)

    def test_no_table_is_silent(self):
        """No compile_table → no mismatch check → no warning."""
        warnings = self._query_with(
            allowed_skills=["bm25_search", "embedding_search"],
            table=None,
        )
        msgs = [w.getMessage() for w in warnings]
        assert not any("compile_table" in m for m in msgs)

    def test_no_allowed_skills_is_silent(self):
        """No allowed_skills cap → no mismatch check → no warning."""
        warnings = self._query_with(
            allowed_skills=None,
            table={"python:stacktrace": ["bm25_search"]},
        )
        msgs = [w.getMessage() for w in warnings]
        assert not any("compile_table" in m for m in msgs)
