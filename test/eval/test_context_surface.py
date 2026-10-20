# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Surface/body contracts with authenticated source and actual SDK wiring."""

import asyncio
import json
import sys
from dataclasses import make_dataclass
from types import SimpleNamespace

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.mcp.context import ServerContext
from codenib.source_fingerprint import capture_repository_source, fingerprint_repository
from codenib.types import NODE_TYPE_FUNCTION, NodeInfo
from scripts import benchmark_context_claude as claude
from scripts.contextbench_file_surface import GUIDANCE as FILE_GUIDANCE
from scripts.contextbench_file_surface import FileSurfaceContext
from scripts.contextbench_surface import GUIDANCE, SurfaceContext


@pytest.fixture
def indexed(tmp_path, request):
    source = "def answer():\n    return 'BODY_ONLY'\n"
    if getattr(request, "param", None) == "long":
        source = "def answer():\n" + "".join(f"    value = {i}\n" for i in range(260))
    elif getattr(request, "param", None) == "giant":
        source = "#" + "x" * 17000 + "\ndef answer():\n    return 42\n"
    (tmp_path / "a.py").write_text(source)
    identity = fingerprint_repository(tmp_path)
    ctx = ServerContext.load(
        RepoManifest(
            repo_path=str(tmp_path),
            commit="c" * 40,
            source_fingerprint=identity.value,
            file_count=identity.file_count,
        ),
        views=[],
        source_binding=capture_repository_source(tmp_path),
    )
    node = NodeInfo(
        node_name="answer",
        file="a.py",
        start_line=0,
        end_line=len(source.splitlines()) - 1,
        type=NODE_TYPE_FUNCTION,
        content="STALE_INDEX_TEXT",
        score=0.9,
    )
    ctx.vector = SimpleNamespace(search_with_content=lambda **_kw: [node])
    try:
        yield ctx, node
    finally:
        ctx.vector = None
        ctx.close()


def exchange(name, arguments, text, call_id="c"):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": text},
        {"role": "assistant", "content": "Evidence received"},
    ]


def predict(ctx, messages):
    return claude.native.contextbench_prediction(
        instance_id="case", messages=messages, repo_path=ctx.manifest.repo_path
    )["traj_data"]["pred_spans"]


def test_surface_issues_stable_bounded_handles_without_crediting_unread_body(indexed):
    ctx, _node = indexed
    surface = SurfaceContext(ctx, observed_messages=lambda: [])
    text = surface.execute(query="answer implementation")
    data = json.loads(text)[0]
    entry = data["entries"][0]
    assert entry["declaration_excerpt"] == "def answer():"
    assert entry["lines"] == [1, 2]
    assert "BODY_ONLY" not in text and "STALE_INDEX_TEXT" not in text
    assert data["graph_available"] is False
    assert predict(ctx, exchange("explore_context", {"query": "answer"}, text)) == {}
    other = SurfaceContext(ctx, observed_messages=lambda: [])
    assert json.loads(other.execute(query="answer"))[0]["entries"][0] == entry
    bounded = SurfaceContext(ctx, observed_messages=lambda: [], token_budget=256)
    reply = bounded.execute(query="answer")
    assert len(bounded.encoder.encode(reply, disallowed_special=())) <= 256


def test_body_reads_actual_source_deduplicates_confirmed_history_and_rejects_stale(
    indexed, tmp_path
):
    ctx, _node = indexed
    history = exchange("read", {"file_path": "a.py"}, "1 | def answer():", "read1")
    surface = SurfaceContext(ctx, observed_messages=lambda: history)
    handle = json.loads(surface.execute(query="answer"))[0]["entries"][0]["handle"]
    args = {"view": "body", "handle": handle}
    body = surface.execute(**args)
    data = json.loads(body)[0]
    assert "BODY_ONLY" in data["content"] and "def answer" not in data["content"]
    assert data["previously_seen_lines_omitted"] == 1
    assert data["previous_tool_calls"] == ["read1"]
    assert data["next_start_line"] is None
    assert predict(ctx, exchange("explore_context", args, body)) == {
        "a.py": [{"start": 2, "end": 2}]
    }
    history.extend(exchange("explore_context", args, body, "body1"))
    assert json.loads(surface.execute(**args))[0]["content"] == ""
    with pytest.raises(ValueError, match="Unknown handle"):
        surface.execute(view="body", handle="../../a.py")
    with pytest.raises(ValueError, match="outside"):
        surface.execute(**args, start_line=3)
    ctx.manifest.commit = "d" * 40
    with pytest.raises(ValueError, match="snapshot"):
        surface.execute(**args)
    ctx.manifest.commit = "c" * 40
    (tmp_path / "a.py").write_text("def answer():\n    return 'NEW_VERSION'\n")
    with pytest.raises((RuntimeError, ValueError, OSError)):
        surface.execute(**args)


@pytest.mark.parametrize("indexed", ["long"], indirect=True)
def test_large_function_has_explicit_pagination_without_enclosing_range_credit(indexed):
    ctx, _node = indexed
    surface = SurfaceContext(ctx, observed_messages=lambda: [])
    handle = json.loads(surface.execute(query="answer"))[0]["entries"][0]["handle"]
    args = {"view": "body", "handle": handle}
    first = surface.execute(**args)
    assert json.loads(first)[0]["next_start_line"] == 201
    assert predict(ctx, exchange("explore_context", args, first)) == {
        "a.py": [{"start": 1, "end": 200}]
    }
    rest = surface.execute(**args, start_line=201)
    assert json.loads(rest)[0]["next_start_line"] is None
    assert predict(ctx, exchange("explore_context", args, rest)) == {
        "a.py": [{"start": 201, "end": 261}]
    }


@pytest.mark.parametrize("indexed", ["giant"], indirect=True)
def test_oversized_partial_line_is_explicit_unavailable_and_cannot_gain_credit(indexed):
    ctx, _node = indexed
    surface = SurfaceContext(ctx, observed_messages=lambda: [])
    handle = json.loads(surface.execute(query="answer"))[0]["entries"][0]["handle"]
    args = {"view": "body", "handle": handle}
    first = surface.execute(**args)
    data = json.loads(first)[0]
    assert data["content"] == ""
    assert data["partial_line_not_credited"] is True
    assert data["unavailable_line"] == 1 and data["next_start_line"] == 2
    assert predict(ctx, exchange("explore_context", args, first)) == {}
    rest = surface.execute(**args, start_line=2)
    assert predict(ctx, exchange("explore_context", args, rest)) == {
        "a.py": [{"start": 2, "end": 3}]
    }


@pytest.mark.parametrize("source_only", [False, True])
def test_sdk_surface_then_body_is_real_tool_delivery(
    indexed, tmp_path, monkeypatch, source_only
):
    ctx, _node = indexed
    row = {"instance_id": "case", "messages": [], "sdk_events": []}
    if source_only:
        ctx.vector = None
    surface_type = FileSurfaceContext if source_only else SurfaceContext
    guidance = FILE_GUIDANCE if source_only else GUIDANCE
    surface = surface_type(ctx, observed_messages=lambda: tuple(row["messages"]))

    def event(kind, **fields):
        return make_dataclass(kind, [(k, object) for k in fields])(**fields)

    async def query(*, prompt, options):
        assert guidance in options.system_prompt
        handlers = dict(options.mcp_servers["source"])
        handle = None
        for call_id, args in [
            (
                "surface",
                (
                    {"file_path": "a.py"}
                    if source_only
                    else {"query": "answer implementation"}
                ),
            ),
            ("body", {"view": "body"}),
        ]:
            if call_id == "body":
                args["handle"] = handle
            yield event(
                "AssistantMessage",
                content=[
                    {
                        "id": call_id,
                        "name": "mcp__source__explore_context",
                        "input": args,
                    }
                ],
                model="test-model",
            )
            result = await handlers["explore_context"](args)
            assert not result["isError"]
            text = result["content"][0]["text"]
            if call_id == "surface":
                assert "BODY_ONLY" not in text
                handle = json.loads(text)[0]["entries"][0]["handle"]
            else:
                assert "BODY_ONLY" in text
            yield event(
                "UserMessage",
                content=[{"tool_use_id": call_id, "content": result["content"]}],
            )
        yield event(
            "AssistantMessage", content=[{"text": "Checked source"}], model="test-model"
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
            tool=lambda name, description, schema: lambda handler: (name, handler),
            query=query,
        ),
    )
    asyncio.run(
        claude.retrieve(
            repo=tmp_path,
            query_text="Find answer",
            row=row,
            checkpoint=lambda: None,
            cli="test",
            model="test-model",
            max_turns=5,
            context_tool=surface.tool(),
            context_guidance=guidance,
        )
    )
    assert row["status"] == "ok"
    assert predict(ctx, row["messages"]) == {"a.py": [{"start": 1, "end": 2}]}
