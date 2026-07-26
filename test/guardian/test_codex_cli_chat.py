# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for Codex-backed chat adapters."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from codeminer.guardian.llm.codex_cli_chat import CodexCliChat, CodexSdkChat


def _fake_completed(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_call_raw_returns_last_message_and_usage(monkeypatch):
    seen = {}

    def fake_run(cmd, *, input, capture_output, text, timeout):
        seen["cmd"] = cmd
        seen["input"] = input
        out = cmd[cmd.index("--output-last-message") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("answer\n")
        return _fake_completed(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 11,
                        "cached_input_tokens": 3,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 7,
                    },
                }
            )
        )

    monkeypatch.setattr(
        "codeminer.guardian.llm.codex_cli_chat.subprocess.run",
        fake_run,
    )

    llm = CodexCliChat(model="gpt-5.6-luna", cwd="/repo", reasoning_effort="medium")
    response = llm._call_raw([{"role": "user", "content": "hello"}])

    assert response.choices[0].message.content == "answer"
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 12
    assert response.usage.total_tokens == 23
    assert "--json" in seen["cmd"]
    assert "-C" in seen["cmd"]
    assert "model_reasoning_effort=medium" in seen["cmd"]
    assert "[user]\nhello" in seen["input"]


def test_call_raw_maps_json_tool_call(monkeypatch):
    def fake_run(cmd, *, input, capture_output, text, timeout):
        out = cmd[cmd.index("--output-last-message") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "tool_call",
                        "name": "retrieve_evidence",
                        "arguments": {"query": "runner", "top_k": 2},
                    }
                )
            )
        return _fake_completed()

    monkeypatch.setattr(
        "codeminer.guardian.llm.codex_cli_chat.subprocess.run",
        fake_run,
    )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "retrieve_evidence",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    response = CodexCliChat(model="gpt-5.6-luna")._call_raw(
        [{"role": "user", "content": "inspect"}],
        tools=tools,
        tool_choice="auto",
    )

    tool_call = response.choices[0].message.tool_calls[0]
    assert tool_call.function.name == "retrieve_evidence"
    assert json.loads(tool_call.function.arguments) == {"query": "runner", "top_k": 2}


def test_call_raw_maps_json_final_when_tools_are_available(monkeypatch):
    def fake_run(cmd, *, input, capture_output, text, timeout):
        out = cmd[cmd.index("--output-last-message") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "final", "content": "verdict: inconclusive"}))
        return _fake_completed()

    monkeypatch.setattr(
        "codeminer.guardian.llm.codex_cli_chat.subprocess.run",
        fake_run,
    )

    response = CodexCliChat(model="gpt-5.6-luna")._call_raw(
        [{"role": "user", "content": "decide"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "retrieve_evidence", "parameters": {}},
            }
        ],
    )

    assert response.choices[0].message.content == "verdict: inconclusive"
    assert response.choices[0].message.tool_calls == []


def test_guardian_factory_selects_sdk_when_installed(monkeypatch):
    import importlib.util

    from codeminer.guardian.cycle import GuardianConfig, _make_llm

    monkeypatch.delenv("CODEX_FORCE_AUTH_JSON", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name):
        if name == "openai_codex":
            return object()
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    llm = _make_llm(GuardianConfig(repo_path="/repo", llm_model="codex:gpt-5.6-luna"))

    assert isinstance(llm, CodexSdkChat)
    assert llm.model == "gpt-5.6-luna"
    assert llm.cwd == "/repo"


def test_guardian_factory_selects_sdk_for_subscription_auth(monkeypatch):
    import importlib.util

    from codeminer.guardian.cycle import GuardianConfig, _make_llm

    monkeypatch.setenv("CODEX_FORCE_AUTH_JSON", "1")
    monkeypatch.delenv("CODEMINER_CODEX_TRANSPORT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name):
        if name == "openai_codex":
            return object()
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    llm = _make_llm(GuardianConfig(repo_path="/repo", llm_model="codex:gpt-5.6-luna"))

    assert isinstance(llm, CodexSdkChat)
    assert llm.model == "gpt-5.6-luna"
    assert llm.cwd == "/repo"


def test_guardian_factory_selects_cli_when_requested(monkeypatch):
    import importlib.util

    from codeminer.guardian.cycle import GuardianConfig, _make_llm

    monkeypatch.setenv("CODEMINER_CODEX_TRANSPORT", "cli")
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name):
        if name == "openai_codex":
            return object()
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    llm = _make_llm(GuardianConfig(repo_path="/repo", llm_model="codex:gpt-5.6-luna"))

    assert isinstance(llm, CodexCliChat)
    assert llm.model == "gpt-5.6-luna"
    assert llm.cwd == "/repo"


def test_guardian_factory_falls_back_to_cli_without_sdk(monkeypatch):
    import importlib.util

    from codeminer.guardian.cycle import GuardianConfig, _make_llm

    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name):
        if name == "openai_codex":
            return None
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    llm = _make_llm(GuardianConfig(repo_path="/repo", llm_model="codex:gpt-5.6-luna"))

    assert isinstance(llm, CodexCliChat)
    assert llm.model == "gpt-5.6-luna"
    assert llm.cwd == "/repo"


def test_cycle_usage_merge_combines_outer_and_inner_usage():
    from codeminer.guardian.cycle import _merge_llm_usage
    from codeminer.guardian.investigator import LLMUsage

    total = LLMUsage()
    outer = LLMUsage(
        prompt_tokens=10,
        cached_input_tokens=6,
        completion_tokens=2,
        total_tokens=12,
    )
    inner = LLMUsage(
        prompt_tokens=30,
        cached_input_tokens=20,
        completion_tokens=8,
        total_tokens=38,
    )

    _merge_llm_usage(total, outer, inner)

    assert total.prompt_tokens == 40
    assert total.cached_input_tokens == 26
    assert total.completion_tokens == 10
    assert total.total_tokens == 50


def test_sdk_agent_loop_reuses_one_thread_and_sends_incremental_results(
    monkeypatch,
):
    from codeminer.guardian.llm import agent_loop_session

    seen = {"thread_starts": 0, "prompts": [], "closed": 0}

    class FakeSandbox:
        workspace_write = "workspace-write"

    class FakeApprovalMode:
        deny_all = "deny-all"

    class FakeConfig:
        def __init__(self, **kwargs):
            pass

    class FakeThread:
        def run(self, prompt, **kwargs):
            seen["prompts"].append(prompt)
            answer = (
                {
                    "type": "tool_call",
                    "name": "read_code",
                    "arguments": {"path": "mod.py"},
                }
                if len(seen["prompts"]) == 1
                else {"type": "final", "content": "done"}
            )
            return SimpleNamespace(final_response=json.dumps(answer), usage=None)

    class FakeCodex:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            seen["closed"] += 1

        def thread_start(self, **kwargs):
            seen["thread_starts"] += 1
            return FakeThread()

    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(
            ApprovalMode=FakeApprovalMode,
            Codex=FakeCodex,
            CodexConfig=FakeConfig,
            Sandbox=FakeSandbox,
        ),
    )
    tools = [
        {
            "type": "function",
            "function": {"name": "read_code", "parameters": {}},
        }
    ]
    initial = [{"role": "user", "content": "inspect the commit"}]
    llm = CodexSdkChat(model="gpt-5.6-luna", cwd="/repo")

    with agent_loop_session(llm) as session:
        first = session._call_raw(initial, tools=tools)
        call = first.choices[0].message.tool_calls[0]
        continued = [
            *initial,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": "def parse(): pass",
            },
        ]
        second = session._call_raw(continued, tools=tools)

    assert second.choices[0].message.content == "done"
    assert seen["thread_starts"] == 1
    assert seen["closed"] == 1
    assert "inspect the commit" in seen["prompts"][0]
    assert "def parse(): pass" in seen["prompts"][1]
    assert "inspect the commit" not in seen["prompts"][1]


def test_sdk_usage_prefers_last_turn_over_cumulative_total():
    from codeminer.guardian.llm.codex_cli_chat import _usage_from_sdk

    result = SimpleNamespace(
        usage=SimpleNamespace(
            total=SimpleNamespace(
                input_tokens=100,
                cached_input_tokens=70,
                output_tokens=20,
                reasoning_output_tokens=10,
            ),
            last=SimpleNamespace(
                input_tokens=30,
                cached_input_tokens=20,
                output_tokens=4,
                reasoning_output_tokens=2,
            ),
        )
    )

    usage = _usage_from_sdk(result)

    assert usage.prompt_tokens == 30
    assert usage.cached_input_tokens == 20
    assert usage.completion_tokens == 6
    assert usage.total_tokens == 36


def test_sdk_call_raw_uses_codex_sdk(monkeypatch):
    seen = {}

    class FakeSandbox:
        workspace_write = "workspace-write"

    class FakeApprovalMode:
        deny_all = "deny-all"

    class FakeConfig:
        def __init__(self, **kwargs):
            seen["config"] = kwargs

    class FakeThread:
        def run(self, prompt, **kwargs):
            seen["prompt"] = prompt
            seen["run_kwargs"] = kwargs
            usage = SimpleNamespace(
                total=SimpleNamespace(
                    input_tokens=13,
                    cached_input_tokens=4,
                    output_tokens=6,
                    reasoning_output_tokens=8,
                    # Older SDKs can omit reasoning from this aggregate.
                    total_tokens=19,
                )
            )
            return SimpleNamespace(final_response="answer\n", usage=usage)

    class FakeCodex:
        def __init__(self, config):
            seen["codex_config"] = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def thread_start(self, **kwargs):
            seen["thread_start"] = kwargs
            return FakeThread()

    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(
            ApprovalMode=FakeApprovalMode,
            Codex=FakeCodex,
            CodexConfig=FakeConfig,
            Sandbox=FakeSandbox,
        ),
    )

    llm = CodexSdkChat(
        model="gpt-5.6-luna",
        cwd="/repo",
        codex_bin="/usr/bin/codex",
        reasoning_effort="medium",
    )
    response = llm._call_raw([{"role": "user", "content": "hello"}])

    assert response.choices[0].message.content == "answer"
    assert llm.backend_name == "codex-sdk"
    assert llm.transport_history == ["codex-sdk"]
    assert response.usage.prompt_tokens == 13
    assert response.usage.completion_tokens == 14
    assert response.usage.total_tokens == 27
    assert seen["config"]["codex_bin"] == "/usr/bin/codex"
    assert seen["thread_start"]["model"] == "gpt-5.6-luna"
    assert seen["thread_start"]["ephemeral"] is True
    assert seen["run_kwargs"]["effort"] == "medium"
    assert "[user]\nhello" in seen["prompt"]


def test_sdk_default_uses_bundled_runtime(monkeypatch):
    seen = {}

    class FakeSandbox:
        workspace_write = "workspace-write"

    class FakeApprovalMode:
        deny_all = "deny-all"

    class FakeConfig:
        def __init__(self, **kwargs):
            seen["config"] = kwargs

    class FakeThread:
        def run(self, prompt, **kwargs):
            return SimpleNamespace(final_response="answer", usage=None)

    class FakeCodex:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def thread_start(self, **kwargs):
            return FakeThread()

    monkeypatch.delenv("CODEMINER_CODEX_BIN", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(
            ApprovalMode=FakeApprovalMode,
            Codex=FakeCodex,
            CodexConfig=FakeConfig,
            Sandbox=FakeSandbox,
        ),
    )

    CodexSdkChat(model="gpt-5.6-luna")._call_raw([{"role": "user", "content": "hello"}])

    assert seen["config"]["codex_bin"] is None


def test_sdk_call_raw_maps_tool_response(monkeypatch):
    class FakeSandbox:
        workspace_write = "workspace-write"

    class FakeApprovalMode:
        deny_all = "deny-all"

    class FakeConfig:
        def __init__(self, **kwargs):
            pass

    class FakeThread:
        def run(self, prompt, **kwargs):
            return SimpleNamespace(
                final_response=json.dumps(
                    {
                        "type": "tool_call",
                        "name": "retrieve_evidence",
                        "arguments": {"query": "cycle"},
                    }
                ),
                usage=None,
            )

    class FakeCodex:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def thread_start(self, **kwargs):
            return FakeThread()

    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(
            ApprovalMode=FakeApprovalMode,
            Codex=FakeCodex,
            CodexConfig=FakeConfig,
            Sandbox=FakeSandbox,
        ),
    )

    tools = [
        {
            "type": "function",
            "function": {"name": "retrieve_evidence", "parameters": {}},
        }
    ]
    response = CodexSdkChat(model="gpt-5.6-luna")._call_raw(
        [{"role": "user", "content": "inspect"}],
        tools=tools,
    )

    tool_call = response.choices[0].message.tool_calls[0]
    assert tool_call.function.name == "retrieve_evidence"
    assert json.loads(tool_call.function.arguments) == {"query": "cycle"}


def test_sdk_call_raw_falls_back_to_cli(monkeypatch):
    class FakeSandbox:
        workspace_write = "workspace-write"

    class FakeApprovalMode:
        deny_all = "deny-all"

    class FakeConfig:
        def __init__(self, **kwargs):
            pass

    class FakeCodex:
        def __init__(self, config):
            pass

        def __enter__(self):
            raise RuntimeError("app-server unavailable")

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(
            ApprovalMode=FakeApprovalMode,
            Codex=FakeCodex,
            CodexConfig=FakeConfig,
            Sandbox=FakeSandbox,
        ),
    )

    def fake_run(cmd, *, input, capture_output, text, timeout):
        out = cmd[cmd.index("--output-last-message") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("fallback answer")
        return _fake_completed()

    monkeypatch.setattr(
        "codeminer.guardian.llm.codex_cli_chat.subprocess.run",
        fake_run,
    )

    llm = CodexSdkChat(model="gpt-5.6-luna")
    response = llm._call_raw([{"role": "user", "content": "hello"}])

    assert response.choices[0].message.content == "fallback answer"
    assert llm.backend_name == "codex-cli-fallback"
    assert llm.transport_history == ["codex-sdk", "codex-cli-fallback"]


def test_sdk_call_raw_marks_unavailable_when_fallback_fails(monkeypatch):
    class FakeSandbox:
        workspace_write = "workspace-write"

    class FakeApprovalMode:
        deny_all = "deny-all"

    class FakeConfig:
        def __init__(self, **kwargs):
            pass

    class FakeCodex:
        def __init__(self, config):
            pass

        def __enter__(self):
            raise RuntimeError("app-server unavailable")

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setitem(
        sys.modules,
        "openai_codex",
        SimpleNamespace(
            ApprovalMode=FakeApprovalMode,
            Codex=FakeCodex,
            CodexConfig=FakeConfig,
            Sandbox=FakeSandbox,
        ),
    )

    def fake_run(cmd, *, input, capture_output, text, timeout):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(
        "codeminer.guardian.llm.codex_cli_chat.subprocess.run",
        fake_run,
    )

    llm = CodexSdkChat(model="gpt-5.6-luna")
    try:
        llm._call_raw([{"role": "user", "content": "hello"}])
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected fallback failure")

    assert llm.backend_name == "codex-unavailable"
    assert llm.transport_history == [
        "codex-sdk",
        "codex-cli-fallback",
        "codex-unavailable",
    ]
