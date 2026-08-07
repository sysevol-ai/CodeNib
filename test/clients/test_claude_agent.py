# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def claude_module(monkeypatch):
    calls = SimpleNamespace(options=None, prompt=None, total_cost_usd=0.01)
    sdk = ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ResultMessage:
        structured_output = {
            "locations": [
                {
                    "name": "target()",
                    "type": "function",
                    "file_path": "module.py",
                    "line_start": 1,
                    "line_end": 2,
                    "action": "modify",
                    "description": "Relevant implementation",
                }
            ]
        }
        result = None
        usage = {"input_tokens": 10, "output_tokens": 2}
        num_turns = 1
        total_cost_usd = 0.01
        duration_ms = 20

    async def query(*, prompt, options):
        calls.prompt = prompt
        calls.options = options
        message = ResultMessage()
        message.total_cost_usd = calls.total_cost_usd
        yield message

    sdk.ClaudeAgentOptions = ClaudeAgentOptions
    sdk.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    sys.modules.pop("codenib.clients.claude_agent", None)
    module = importlib.import_module("codenib.clients.claude_agent")
    try:
        yield module, calls
    finally:
        sys.modules.pop("codenib.clients.claude_agent", None)


def test_default_session_exposes_only_read_only_tools(claude_module, tmp_path):
    module, calls = claude_module
    agent = module.ClaudeLocAgent(max_turns=2)

    result = asyncio.run(agent.locate_code("find target", str(tmp_path)))

    assert result.success is True
    assert [location.name for location in result.locations] == ["target()"]
    options = calls.options
    assert options.tools == ["Read", "Glob", "Grep"]
    assert options.allowed_tools == options.tools
    assert options.permission_mode == "dontAsk"
    assert {
        "Bash",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Task",
        "WebFetch",
        "WebSearch",
        "Write",
    } <= set(options.disallowed_tools)


def test_session_ignores_external_extensions(claude_module, tmp_path):
    module, calls = claude_module
    agent = module.ClaudeLocAgent(allowed_tools=["Read"])

    asyncio.run(agent.locate_code("find target", str(tmp_path)))

    options = calls.options
    assert options.tools == ["Read"]
    assert options.setting_sources == []
    assert options.skills == []
    assert options.plugins == []
    assert options.agents == {}
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True


def test_constructor_rejects_sdk_without_isolation_options(claude_module):
    module, _calls = claude_module

    class LegacyClaudeAgentOptions:
        __slots__ = ("allowed_tools", "permission_mode")

        def __init__(self, *, allowed_tools, permission_mode):
            self.allowed_tools = allowed_tools
            self.permission_mode = permission_mode

    module.ClaudeAgentOptions = LegacyClaudeAgentOptions

    with pytest.raises(RuntimeError, match="cannot enforce.*read-only"):
        module.ClaudeLocAgent()


def test_missing_cost_does_not_discard_successful_result(claude_module, tmp_path):
    module, calls = claude_module
    calls.total_cost_usd = None

    result = asyncio.run(
        module.ClaudeLocAgent().locate_code("find target", str(tmp_path))
    )

    assert result.success is True
    assert result.usage["total_cost_usd"] is None


@pytest.mark.parametrize(
    "tool",
    ["Bash", "Task", "Write", "Edit", "NotebookEdit", "mcp__custom__read"],
)
def test_constructor_rejects_tools_outside_the_read_only_contract(claude_module, tool):
    module, _calls = claude_module

    with pytest.raises(ValueError, match="supports only read-only tools"):
        module.ClaudeLocAgent(allowed_tools=["Read", tool])


@pytest.mark.parametrize("permission_mode", ["bypassPermissions", "acceptEdits"])
def test_constructor_rejects_unsafe_permission_modes(claude_module, permission_mode):
    module, _calls = claude_module

    with pytest.raises(ValueError, match="permission_mode"):
        module.ClaudeLocAgent(permission_mode=permission_mode)


@pytest.mark.parametrize("max_turns", [0, -1, True])
def test_constructor_rejects_invalid_turn_limits(claude_module, max_turns):
    module, _calls = claude_module

    with pytest.raises(ValueError, match="max_turns"):
        module.ClaudeLocAgent(max_turns=max_turns)
