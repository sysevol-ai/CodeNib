# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the system-prompt ENVIRONMENT block (issue #133)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.compiler.params import SessionContext
from codeminer.llm.litellm_chat import LiteLLMChat


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def test_environment_block_lists_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x")
    (tmp_path / ".git").mkdir()  # should be skipped
    (tmp_path / "node_modules").mkdir()  # should be skipped

    llm = MagicMock(spec=LiteLLMChat)
    runner = AgentRunner(
        llm,
        SkillRegistry(),
        session_ctx=SessionContext(
            repo_path=str(tmp_path), primary_language="python", repo_size=42
        ),
    )
    prompt = runner.system_prompt
    assert "<environment>" in prompt
    assert str(tmp_path) in prompt
    assert "primary_language: python" in prompt
    assert "repo_size: 42 files" in prompt
    assert "src/" in prompt and "README.md" in prompt
    # Noise dirs are filtered out.
    assert ".git" not in prompt.split("top-level entries:")[1]
    assert "node_modules" not in prompt


def test_no_environment_block_without_repo_path():
    llm = MagicMock(spec=LiteLLMChat)
    runner = AgentRunner(llm, SkillRegistry())  # no session_ctx
    # The base prompt mentions "<environment>" in prose; the *rendered* block
    # is identified by its repo_path line, which must be absent here.
    assert "repo_path:" not in runner.system_prompt


def test_workflow_prompt_teaches_graph_and_files():
    llm = MagicMock(spec=LiteLLMChat)
    runner = AgentRunner(llm, SkillRegistry())
    p = runner.system_prompt
    assert "find_callers" in p and "find_callees" in p  # graph navigation verbs
    assert "file_search" in p and "file_read" in p
    assert "file_search" in p and "file_read" in p
