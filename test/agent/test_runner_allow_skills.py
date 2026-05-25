# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``allow_skills`` / ``allow`` filtering on AgentRunner + tool_schema."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from codeminer.agent.resource_guard import PreflightReport
from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codeminer.agent.skills.defaults import DEFAULT_SKILL_IDS
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.agent.tool_schema import registry_to_tools
from codeminer.llm.litellm_chat import LiteLLMChat


def _swept(tools) -> set:
    """Tool names minus the always-on default layer (file_read/file_search).

    The default tool layer is unioned into every AgentRunner tool set
    regardless of allow_skills/exclude, so allow-filtering assertions test
    the *swept* skills with the defaults removed.
    """
    return {t["function"]["name"] for t in tools} - set(DEFAULT_SKILL_IDS)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def _register(reg: SkillRegistry, skill_id: str) -> None:
    reg.register(
        SkillMetadata(
            skill_id=skill_id,
            skill_type=SkillType.CUSTOM,
            inputs=[SkillInputSpec(name="q", type_hint="str", required=True)],
            outputs=SkillOutputSpec(type_hint="str"),
            executor_fn=lambda q, _sid=skill_id: f"{_sid}: {q}",
        )
    )


@pytest.fixture()
def three_skill_registry():
    """Registry with three distinct skills: a, b, c."""
    reg = SkillRegistry()
    for sid in ("a", "b", "c"):
        _register(reg, sid)
    return reg


# ---------------------------------------------------------------------------
# registry_to_tools ``allow`` / ``exclude`` filtering
# ---------------------------------------------------------------------------


class TestRegistryToToolsAllow:
    def test_allow_none_returns_all(self, three_skill_registry):
        tools = registry_to_tools(three_skill_registry)
        names = {t["function"]["name"] for t in tools}
        assert names == {"a", "b", "c"}

    def test_allow_subset(self, three_skill_registry):
        tools = registry_to_tools(three_skill_registry, allow={"a", "b"})
        names = {t["function"]["name"] for t in tools}
        assert names == {"a", "b"}

    def test_allow_empty_set_returns_nothing(self, three_skill_registry):
        """An empty allow set is still a set, not None → excludes everything."""
        tools = registry_to_tools(three_skill_registry, allow=set())
        assert tools == []

    def test_allow_unknown_skill_is_ignored(self, three_skill_registry):
        tools = registry_to_tools(three_skill_registry, allow={"a", "does_not_exist"})
        names = {t["function"]["name"] for t in tools}
        assert names == {"a"}

    def test_exclude_applied_after_allow(self, three_skill_registry):
        """exclude wins when a skill is in both allow and exclude."""
        tools = registry_to_tools(
            three_skill_registry, allow={"a", "b", "c"}, exclude={"b"}
        )
        names = {t["function"]["name"] for t in tools}
        assert names == {"a", "c"}

    def test_exclude_only(self, three_skill_registry):
        tools = registry_to_tools(three_skill_registry, exclude={"c"})
        names = {t["function"]["name"] for t in tools}
        assert names == {"a", "b"}


# ---------------------------------------------------------------------------
# AgentRunner ``allow_skills`` wiring
# ---------------------------------------------------------------------------


class TestAgentRunnerAllowSkills:
    def test_allow_skills_filters_tools(self, three_skill_registry):
        """Runner's tool schema list only contains allowed skills."""
        llm = MagicMock(spec=LiteLLMChat)
        runner = AgentRunner(llm, three_skill_registry, allow_skills={"a", "b"})
        assert _swept(runner.tools) == {"a", "b"}
        # Defaults are always present on top of the allow set.
        names = {t["function"]["name"] for t in runner.tools}
        assert set(DEFAULT_SKILL_IDS) <= names

    def test_allow_skills_none_includes_all(self, three_skill_registry):
        llm = MagicMock(spec=LiteLLMChat)
        runner = AgentRunner(llm, three_skill_registry)
        assert _swept(runner.tools) == {"a", "b", "c"}

    def test_allow_skills_single(self, three_skill_registry):
        llm = MagicMock(spec=LiteLLMChat)
        runner = AgentRunner(llm, three_skill_registry, allow_skills={"c"})
        assert _swept(runner.tools) == {"c"}

    def test_allow_and_exclude_combined(self, three_skill_registry):
        """exclude is applied on top of allow; overlap is excluded."""
        llm = MagicMock(spec=LiteLLMChat)
        runner = AgentRunner(
            llm,
            three_skill_registry,
            allow_skills={"a", "b", "c"},
            exclude_skills={"a"},
        )
        assert _swept(runner.tools) == {"b", "c"}

    def test_allow_skills_empty_set_falls_back_to_full_registry(
        self, three_skill_registry
    ):
        """Issue #149 pins the empty-allowlist contract: empty → full
        registry + WARN log. Previously empty set yielded zero tools,
        which would stall the agent silently on a compile_table miss.
        """
        import logging

        # The project logger uses propagate=False, so caplog can't see it;
        # attach our own list handler to the runner's logger directly.
        captured: list[logging.LogRecord] = []
        runner_logger = logging.getLogger("codeminer.agent.runner")

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _ListHandler(level=logging.WARNING)
        runner_logger.addHandler(handler)
        try:
            llm = MagicMock(spec=LiteLLMChat)
            runner = AgentRunner(llm, three_skill_registry, allow_skills=set())
        finally:
            runner_logger.removeHandler(handler)

        assert _swept(runner.tools) == {"a", "b", "c"}
        assert any("empty allow_skills" in r.getMessage() for r in captured)

    def test_allow_skills_unknown_id_ignored(self, three_skill_registry):
        llm = MagicMock(spec=LiteLLMChat)
        runner = AgentRunner(llm, three_skill_registry, allow_skills={"a", "ghost"})
        assert _swept(runner.tools) == {"a"}

    def test_include_default_tools_false_withholds_defaults(self, three_skill_registry):
        """The cost-study 'structured' condition: defaults are withheld so the
        agent must use the swept skills only."""
        llm = MagicMock(spec=LiteLLMChat)
        runner = AgentRunner(
            llm,
            three_skill_registry,
            allow_skills={"a"},
            include_default_tools=False,
        )
        names = {t["function"]["name"] for t in runner.tools}
        assert names == {"a"}
        assert not (set(DEFAULT_SKILL_IDS) & names)


# ---------------------------------------------------------------------------
# allow_skills × ResourceGuard interaction
# ---------------------------------------------------------------------------


class TestAllowSkillsWithResourceGuard:
    """Pin the current behaviour when a user-allowed skill is marked
    unavailable by the ResourceGuard: the guard's ``unavailable`` set is
    unioned into ``exclude``, which is applied **after** ``allow``, so the
    skill is silently dropped from ``runner.tools``.
    """

    def test_guard_unavailable_overrides_allow(self, three_skill_registry):
        """A skill in ``allow_skills`` but marked unavailable by the guard
        is excluded (exclude wins over allow)."""
        fake_guard = MagicMock()
        fake_guard.preflight.return_value = PreflightReport(
            available={"a", "c"},
            unavailable={"b"},
            warnings=["Skill 'b' unavailable: missing indexes [bm25]"],
        )

        with patch(
            "codeminer.agent.resource_guard.ResourceGuard",
            return_value=fake_guard,
        ):
            llm = MagicMock(spec=LiteLLMChat)
            runner = AgentRunner(
                llm,
                three_skill_registry,
                allow_skills={"a", "b"},
                manifest=MagicMock(name="manifest"),
            )

        # 'b' is silently dropped even though the user allowed it.
        assert _swept(runner.tools) == {"a"}
        # Warning from the guard is surfaced in the system prompt.
        assert "missing indexes" in runner.system_prompt

    def test_guard_all_unavailable_with_allow_yields_only_defaults(
        self, three_skill_registry
    ):
        """Every *swept* skill is unavailable → only the always-on default
        tool layer remains (the guard can never drop file_read/file_search)."""
        fake_guard = MagicMock()
        fake_guard.preflight.return_value = PreflightReport(
            unavailable={"a", "b", "c"},
            warnings=["all unavailable"],
        )

        with patch(
            "codeminer.agent.resource_guard.ResourceGuard",
            return_value=fake_guard,
        ):
            llm = MagicMock(spec=LiteLLMChat)
            runner = AgentRunner(
                llm,
                three_skill_registry,
                allow_skills={"a", "b"},
                manifest=MagicMock(name="manifest"),
            )

        assert _swept(runner.tools) == set()
        names = {t["function"]["name"] for t in runner.tools}
        assert names == set(DEFAULT_SKILL_IDS)

    def test_guard_does_not_expand_allow(self, three_skill_registry):
        """A skill marked ``available`` by the guard but NOT in
        ``allow_skills`` stays filtered out — the guard cannot widen the
        allowlist."""
        fake_guard = MagicMock()
        fake_guard.preflight.return_value = PreflightReport(
            available={"a", "b", "c"},
            unavailable=set(),
            warnings=[],
        )

        with patch(
            "codeminer.agent.resource_guard.ResourceGuard",
            return_value=fake_guard,
        ):
            llm = MagicMock(spec=LiteLLMChat)
            runner = AgentRunner(
                llm,
                three_skill_registry,
                allow_skills={"a"},
                manifest=MagicMock(name="manifest"),
            )

        assert _swept(runner.tools) == {"a"}
