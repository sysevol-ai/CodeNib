# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""AgentRunner wiring for optional static LSP route startup context."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codenib.agent.route_context import fingerprint_lsp_route_nodes
from codenib.agent.runner import AgentRunner
from codenib.agent.skills.core import (
    SkillInputSpec,
    SkillMetadata,
    SkillOutputSpec,
    SkillType,
)
from codenib.agent.skills.registry import SkillRegistry
from codenib.llm.litellm_chat import LiteLLMChat
from codenib.types import QueriedNode


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def _make_llm():
    return MagicMock(spec=LiteLLMChat)


def _make_response(content=None, tool_calls=None):
    msg = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _lsp_route_registry(calls):
    def executor(**kwargs):
        calls.append(kwargs)
        return [
            QueriedNode(
                node_name="svc.HandleRequest",
                type="method",
                file="svc.py",
                start_line=9,
                end_line=11,
                content="route endpoint: direct seed HandleRequest",
            )
        ]

    reg = SkillRegistry()
    reg.register(
        SkillMetadata(
            skill_id="lsp_route",
            skill_type=SkillType.EXPAND,
            inputs=[
                SkillInputSpec(
                    name="symbols",
                    type_hint="List[str]",
                    required=True,
                ),
                SkillInputSpec(name="query", type_hint="str", required=False),
            ],
            outputs=SkillOutputSpec(type_hint="List[QueriedNode]"),
            executor_fn=executor,
        )
    )
    return reg


def test_runner_adds_lsp_route_guidance_when_dynamic_tool_is_exposed():
    runner = AgentRunner(
        _make_llm(),
        _lsp_route_registry([]),
        include_default_tools=False,
        allow_skills={"lsp_route"},
    )

    assert "Dynamic LSP route tool:" in runner.system_prompt
    assert "lsp_route(symbols=[...], query=<original request>, top_k=5)" in (
        runner.system_prompt
    )
    assert "symbols=[]" in runner.system_prompt


def test_runner_skips_lsp_route_guidance_when_tool_is_not_exposed():
    runner = AgentRunner(
        _make_llm(),
        _lsp_route_registry([]),
        include_default_tools=False,
        allow_skills={"missing"},
        enable_lsp_route_context=True,
    )

    assert "Dynamic LSP route tool:" not in runner.system_prompt


def test_runner_injects_opt_in_lsp_route_context_before_first_llm_call():
    calls = []
    llm = _make_llm()
    llm._call_raw.return_value = _make_response(content="done")

    result = AgentRunner(
        llm,
        _lsp_route_registry(calls),
        include_default_tools=False,
        enable_lsp_route_context=True,
        lsp_route_seed_limit=3,
        lsp_route_top_k=5,
        lsp_route_include_neighbors=False,
        force_localization_contract=False,
    ).run("Fix `HandleRequest` default config")

    assert result.answer == "done"
    assert result.tool_calls == []
    assert calls == [
        {
            "symbols": ["HandleRequest"],
            "query": "Fix `HandleRequest` default config",
            "top_k": 5,
            "include_neighbors": False,
        }
    ]
    messages = llm._call_raw.call_args.args[0]
    user_message = next(msg for msg in messages if msg["role"] == "user")["content"]
    assert "Fix `HandleRequest` default config" in user_message
    assert "# Static LSP route hints" in user_message
    assert "svc.py:10-12 svc.HandleRequest [method]" in user_message

    assert result.trace is not None
    route_events = [
        event for event in result.trace.events if event.kind == "lsp_route_context"
    ]
    assert route_events[0].data["status"] == "offered"
    assert route_events[0].data["seeds"] == ["HandleRequest"]
    assert route_events[0].data["seed_policy"] == "all"
    assert route_events[0].data["route_count"] == 1
    assert isinstance(route_events[0].data["duration_ms"], float)
    assert route_events[0].data["route_args"] == {
        "symbols": ["HandleRequest"],
        "query": "Fix `HandleRequest` default config",
        "top_k": 5,
        "include_neighbors": False,
    }
    assert route_events[0].data["route_fingerprint"] == fingerprint_lsp_route_nodes(
        [
            QueriedNode(
                node_name="svc.HandleRequest",
                type="method",
                file="svc.py",
                start_line=9,
                end_line=11,
                content="route endpoint: direct seed HandleRequest",
            )
        ]
    )

    route_entries = [
        entry for entry in result.trace.context if entry.source == "lsp_route"
    ]
    assert route_entries[0].state == "offered"
    assert route_entries[0].provenance == {
        "kind": "initial_context",
        "tool": "lsp_route",
    }
    assert route_entries[0].metadata["seed_policy"] == "all"
    assert route_entries[0].metadata["route_count"] == 1
    assert isinstance(route_entries[0].metadata["duration_ms"], float)
    assert route_entries[0].metadata["route_args"] == route_events[0].data["route_args"]
    assert (
        route_entries[0].metadata["route_fingerprint"]
        == route_events[0].data["route_fingerprint"]
    )


def test_runner_specific_lsp_route_seed_policy_skips_generic_seed():
    calls = []
    llm = _make_llm()
    llm._call_raw.return_value = _make_response(content="done")

    result = AgentRunner(
        llm,
        _lsp_route_registry(calls),
        include_default_tools=False,
        enable_lsp_route_context=True,
        lsp_route_seed_policy="specific",
        force_localization_contract=False,
    ).run("Fix `format` behavior")

    assert calls == []
    messages = llm._call_raw.call_args.args[0]
    user_message = next(msg for msg in messages if msg["role"] == "user")["content"]
    assert user_message == "Fix `format` behavior"

    assert result.trace is not None
    route_events = [
        event for event in result.trace.events if event.kind == "lsp_route_context"
    ]
    assert route_events[0].data["status"] == "skipped"
    assert route_events[0].data["reason"] == "no_symbol_seeds"
    assert isinstance(route_events[0].data["duration_ms"], float)
    assert list(result.trace.context) == []


def test_runner_can_inject_query_fallback_lsp_route_context_without_seed():
    calls = []
    llm = _make_llm()
    llm._call_raw.return_value = _make_response(content="done")

    result = AgentRunner(
        llm,
        _lsp_route_registry(calls),
        include_default_tools=False,
        enable_lsp_route_context=True,
        lsp_route_seed_policy="specific",
        lsp_route_query_fallback=True,
        force_localization_contract=False,
    ).run("Fix default config behavior")

    assert calls == [
        {
            "symbols": [],
            "query": "Fix default config behavior",
            "top_k": 12,
            "include_neighbors": True,
        }
    ]
    messages = llm._call_raw.call_args.args[0]
    user_message = next(msg for msg in messages if msg["role"] == "user")["content"]
    assert "Seed source: query text" in user_message

    assert result.trace is not None
    route_events = [
        event for event in result.trace.events if event.kind == "lsp_route_context"
    ]
    assert route_events[0].data["status"] == "offered"
    assert route_events[0].data["seeds"] == []
    assert route_events[0].data["seed_source"] == "query"
    assert isinstance(route_events[0].data["duration_ms"], float)


def test_runner_skips_lsp_route_context_when_skill_is_unavailable():
    llm = _make_llm()
    llm._call_raw.return_value = _make_response(content="done")

    result = AgentRunner(
        llm,
        SkillRegistry(),
        include_default_tools=False,
        enable_lsp_route_context=True,
        force_localization_contract=False,
    ).run("Fix `HandleRequest`")

    messages = llm._call_raw.call_args.args[0]
    user_message = next(msg for msg in messages if msg["role"] == "user")["content"]
    assert user_message == "Fix `HandleRequest`"

    assert result.trace is not None
    route_events = [
        event for event in result.trace.events if event.kind == "lsp_route_context"
    ]
    assert route_events[0].data["status"] == "skipped"
    assert route_events[0].data["reason"] == "lsp_route_unavailable"
    assert isinstance(route_events[0].data["duration_ms"], float)
    assert list(result.trace.context) == []
