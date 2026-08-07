# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from enum import Enum
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def codex_module(monkeypatch):
    calls = SimpleNamespace(thread_start=None, prompt=None, output_schema=None)

    claude_sdk = ModuleType("claude_agent_sdk")
    claude_sdk.ClaudeAgentOptions = object
    claude_sdk.query = object
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", claude_sdk)

    codex_sdk = ModuleType("openai_codex")
    codex_types = ModuleType("openai_codex.types")

    class ApprovalMode(Enum):
        deny_all = "deny_all"
        auto_review = "auto_review"

    class SandboxMode(Enum):
        read_only = "read-only"
        workspace_write = "workspace-write"
        danger_full_access = "danger-full-access"

    class Thread:
        async def run(self, prompt, *, output_schema):
            calls.prompt = prompt
            calls.output_schema = output_schema
            return SimpleNamespace(
                final_response=json.dumps(
                    {
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
                ),
                items=[],
                usage=None,
                duration_ms=20,
                status=None,
            )

    class AsyncCodex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def thread_start(self, **kwargs):
            calls.thread_start = kwargs
            return Thread()

    codex_sdk.ApprovalMode = ApprovalMode
    codex_sdk.AsyncCodex = AsyncCodex
    codex_types.SandboxMode = SandboxMode
    monkeypatch.setitem(sys.modules, "openai_codex", codex_sdk)
    monkeypatch.setitem(sys.modules, "openai_codex.types", codex_types)
    sys.modules.pop("codenib.clients.codex_agent", None)

    module = importlib.import_module("codenib.clients.codex_agent")
    try:
        yield module, calls
    finally:
        sys.modules.pop("codenib.clients.codex_agent", None)


def test_default_session_pins_read_only_sandbox(codex_module, tmp_path):
    module, calls = codex_module
    agent = module.CodexLocAgent(model="codex-test")

    result = asyncio.run(agent.locate_code("find target", str(tmp_path)))

    assert result.success is True
    assert [location.name for location in result.locations] == ["target()"]
    options = calls.thread_start
    assert options["sandbox"] is module.SandboxMode.read_only
    assert options["approval_mode"] is module.ApprovalMode.deny_all
    assert options["config"] == {"model_reasoning_effort": "medium"}
    assert options["model"] == "codex-test"


@pytest.mark.parametrize("sandbox_name", ["workspace_write", "danger_full_access"])
def test_constructor_rejects_writable_sandboxes(codex_module, sandbox_name):
    module, _calls = codex_module

    with pytest.raises(ValueError, match="SandboxMode.read_only"):
        module.CodexLocAgent(sandbox=getattr(module.SandboxMode, sandbox_name))


def test_constructor_rejects_auto_review(codex_module):
    module, _calls = codex_module

    with pytest.raises(ValueError, match="ApprovalMode.deny_all"):
        module.CodexLocAgent(approval_mode=module.ApprovalMode.auto_review)


@pytest.mark.parametrize("reasoning_effort", ["", "extreme", None, True])
def test_constructor_rejects_invalid_reasoning_effort(codex_module, reasoning_effort):
    module, _calls = codex_module

    with pytest.raises(ValueError, match="reasoning_effort"):
        module.CodexLocAgent(reasoning_effort=reasoning_effort)
