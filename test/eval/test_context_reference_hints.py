# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph metadata activates source-on-demand without crediting unread bodies."""

import asyncio
import json
import sys
from dataclasses import make_dataclass
from types import SimpleNamespace

import pytest
import tiktoken

from codenib.compiler.manifest import RepoManifest
from codenib.graph.code_graph import CodeGraph
from codenib.mcp.context import ServerContext
from codenib.source_fingerprint import capture_repository_source, fingerprint_repository
from codenib.types import NODE_TYPE_FUNCTION
from scripts import benchmark_context_claude as claude
from scripts.contextbench_codenib_context import make_context_tool
from scripts.contextbench_context_hints import MARKER, ContextHints


@pytest.fixture
def indexed(tmp_path):
    (tmp_path / "a.py").write_text(
        "def target():\n    return other()\n\ndef other():\n    return 'UNREAD_BODY'\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_caller.py").write_text(
        "def test_caller():\n    assert target()\n\ndef test_second():\n    assert target()\n"
    )
    graph = CodeGraph(str(tmp_path))
    graph.add_file_node("a.py")
    graph.add_symbol_node(
        "target",
        line=0,
        scope_start_line=0,
        scope_end_line=1,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.update_current_scope("target", start_line=0, end_line=1)
    graph.add_symbol_reference(
        "other",
        module_path="a.py",
        symbol_type=NODE_TYPE_FUNCTION,
        anchor_file="a.py",
        anchor_line=1,
    )
    graph.add_symbol_node(
        "other",
        line=3,
        scope_start_line=3,
        scope_end_line=4,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.add_file_node("tests/test_caller.py")
    graph.add_symbol_node(
        "test_caller",
        line=0,
        scope_start_line=0,
        scope_end_line=1,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.update_current_scope("test_caller", start_line=0, end_line=1)
    graph.add_symbol_reference(
        "target",
        module_path="a.py",
        symbol_type=NODE_TYPE_FUNCTION,
        anchor_file="tests/test_caller.py",
        anchor_line=1,
    )
    graph.add_symbol_node(
        "test_second",
        line=3,
        scope_start_line=3,
        scope_end_line=4,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.update_current_scope("test_second", start_line=3, end_line=4)
    graph.add_symbol_reference(
        "target",
        module_path="a.py",
        symbol_type=NODE_TYPE_FUNCTION,
        anchor_file="tests/test_caller.py",
        anchor_line=4,
    )
    graph.build_range_indexes()
    identity = fingerprint_repository(tmp_path)
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        commit="c" * 40,
        source_fingerprint=identity.value,
        file_count=identity.file_count,
    )
    ctx = ServerContext.load(
        manifest, views=[], source_binding=capture_repository_source(tmp_path)
    )
    ctx.symbol_graph = graph
    try:
        yield ctx
    finally:
        ctx.vector = None
        ctx.close()


def messages(text):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "read",
                    "function": {
                        "name": "read",
                        "arguments": '{"file_path":"a.py","offset":1,"limit":2}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "read", "content": text},
    ]


def test_hints_use_actual_references_without_reading_or_crediting_neighbor_bodies(
    indexed, tmp_path
):
    original = "1 | def target():\n2 |     return other()"
    history = []
    hints = ContextHints(indexed, repo=tmp_path)
    read = hints.wrap(lambda **_kw: original, observed_messages=lambda: history)
    reply = read(file_path="a.py", offset=1, limit=2)
    assert reply.startswith(original + MARKER)
    data = json.loads(reply.split(MARKER)[1])
    symbol = data["symbols"][0]
    assert symbol["id"] == "target" and symbol["at"] == ["a.py", 1, 2]
    assert symbol["referrers"]["symbols_seen"] == 2
    assert symbol["referrers"]["test_files_seen"] == 1
    assert symbol["referenced"]["symbols_seen"] == 1
    assert "UNREAD_BODY" not in reply and "assert target()" not in reply
    assert (
        len(tiktoken.get_encoding("cl100k_base").encode(reply[len(original) :])) <= 256
    )

    def predict(text):
        return claude.native.contextbench_prediction(
            instance_id="case", messages=messages(text), repo_path=tmp_path
        )

    assert predict(reply) == predict(original)
    assert predict(reply)["traj_data"]["pred_spans"] == {
        "a.py": [{"start": 1, "end": 2}]
    }
    history.extend(messages(reply))
    assert read(file_path="a.py", offset=1, limit=2) == original


def test_hint_scan_bound_and_stale_graph_never_changes_primitive_source(
    indexed, tmp_path
):
    original = "1 | def target():\n2 |     return other()"
    hints = ContextHints(indexed, repo=tmp_path, edge_limit=4)
    read = hints.wrap(lambda **_kw: original, observed_messages=lambda: [])
    reply = read(file_path="a.py", offset=1, limit=2)
    symbol = json.loads(reply.split(MARKER)[1])["symbols"][0]
    assert symbol["referrers"]["scan_complete"] is False
    (tmp_path / "a.py").write_text("def target():\n    return 99\n")
    assert read(file_path="a.py", offset=1, limit=2) == original


def test_missing_graph_is_a_single_truthful_semantic_hint(indexed, tmp_path):
    indexed.symbol_graph = None
    indexed.vector = object()
    history = []
    hints = ContextHints(indexed, repo=tmp_path)
    original = "1 | def target():\n2 |     return other()"
    read = hints.wrap(lambda **_kw: original, observed_messages=lambda: history)
    reply = read(file_path="a.py", offset=1, limit=2)
    data = json.loads(reply.split(MARKER)[1])
    assert data["graph"] == "unavailable" and data["symbols"] == []
    assert data["available_followup"] == "explore_context semantic query"
    history.extend(messages(reply))
    assert read(file_path="a.py", offset=1, limit=2) == original


def test_actual_sdk_hint_can_be_followed_by_real_core_source_on_demand(
    indexed, tmp_path, monkeypatch
):
    row = {"instance_id": "case", "messages": [], "sdk_events": []}

    def event(kind, **fields):
        return make_dataclass(kind, [(k, object) for k in fields])(**fields)

    async def query(*, prompt, options):
        handlers = dict(options.mcp_servers["source"])
        assert "bounded static reference" in options.system_prompt
        args = {"file_path": "a.py", "offset": 1, "limit": 2}
        yield event(
            "AssistantMessage",
            content=[{"id": "r", "name": "mcp__source__read", "input": args}],
            model="test-model",
        )
        first = await handlers["read"](args)
        assert not first["isError"] and MARKER in first["content"][0]["text"]
        assert "assert target()" not in first["content"][0]["text"]
        yield event(
            "UserMessage", content=[{"tool_use_id": "r", "content": first["content"]}]
        )
        args = {
            "query": "references to target",
            "symbols": ["target"],
            "direction": "impact",
            "budget": "fast",
        }
        yield event(
            "AssistantMessage",
            content=[
                {"id": "c", "name": "mcp__source__explore_context", "input": args}
            ],
            model="test-model",
        )
        second = await handlers["explore_context"](args)
        assert not second["isError"]
        yield event(
            "UserMessage", content=[{"tool_use_id": "c", "content": second["content"]}]
        )
        yield event(
            "AssistantMessage",
            content=[{"text": "Checked the referenced test"}],
            model="test-model",
        )
        yield event(
            "ResultMessage",
            subtype="success",
            is_error=False,
            num_turns=3,
            total_cost_usd=0.03,
            result="Done",
        )

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            ClaudeAgentOptions=SimpleNamespace,
            create_sdk_mcp_server=lambda name, tools: tools,
            query=query,
            tool=lambda name, description, schema: lambda handler: (name, handler),
        ),
    )
    asyncio.run(
        claude.retrieve(
            repo=tmp_path,
            query_text="Find missing tests",
            row=row,
            checkpoint=lambda: None,
            cli="test",
            model="test-model",
            max_turns=5,
            context_tool=make_context_tool(indexed),
            read_hints=ContextHints(indexed, repo=tmp_path),
        )
    )
    prediction = claude.native.contextbench_prediction(
        instance_id="case", messages=row["messages"], repo_path=tmp_path
    )
    assert "tests/test_caller.py" in prediction["traj_data"]["pred_spans"]
    assert row["status"] == "ok"
