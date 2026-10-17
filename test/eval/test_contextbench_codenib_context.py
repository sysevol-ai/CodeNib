# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Actual exploration, SDK delivery and scored-source boundaries without models."""

import asyncio
import json
import sys
from dataclasses import make_dataclass
from threading import RLock
from types import SimpleNamespace

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.types import NODE_TYPE_FUNCTION, NodeInfo
from scripts import benchmark_context_claude as claude
from scripts.contextbench_codenib_context import make_context_tool, numbered_context


def context(tmp_path):
    def read(relative, *, max_bytes):
        result = (tmp_path / relative).read_bytes()
        assert len(result) <= max_bytes
        return result

    calls = []

    def search(**kwargs):
        calls.append(kwargs)
        return [
            NodeInfo(
                node_name="answer",
                type=NODE_TYPE_FUNCTION,
                file="a.py",
                start_line=0,
                end_line=1,
                content="stale indexed text",
                score=0.9,
            )
        ]

    return SimpleNamespace(
        manifest=RepoManifest(repo_path=str(tmp_path), commit="abc123"),
        artifact=None,
        source_verified=True,
        source_error=None,
        read_source_bytes=read,
        _view_lock=RLock(),
        vector=SimpleNamespace(search_with_content=search),
        bm25=None,
        grep_jev=None,
        symbol_graph=None,
        lsp_provider=None,
        calls=calls,
    )


def test_sdk_core_context_delivers_live_source_and_exports_only_delivered_lines(
    tmp_path, monkeypatch
):
    (tmp_path / "a.py").write_text("def answer():\n    return 42\n")
    ctx = context(tmp_path)
    spec = make_context_tool(ctx)
    row = {"instance_id": "case", "messages": [], "sdk_events": []}

    def event(kind, **fields):
        return make_dataclass(kind, [(key, object) for key in fields])(**fields)

    async def query(*, prompt, options):
        handlers = dict(options.mcp_servers["source"])
        assert set(handlers) == {"read", "grep", "glob", "explore_context"}
        assert "mcp__source__explore_context" in options.allowed_tools
        assert "Use explore_context" in options.system_prompt
        args = {"query": "answer implementation", "budget": "fast"}
        yield event(
            "AssistantMessage",
            content=[
                {"id": "context", "name": "mcp__source__explore_context", "input": args}
            ],
            model="test-model",
        )
        reply = await handlers["explore_context"](args)
        assert not reply["isError"]
        nodes = json.loads(reply["content"][0]["text"])
        assert nodes[0]["summary"]["retrieval_hits"] == 1
        assert nodes[0]["plan"]["retrieval"]["stages"][0]["engine"] == "dense"
        assert any(d["code"] == "route_unavailable" for d in nodes[0]["diagnostics"])
        assert "stale indexed text" not in reply["content"][0]["text"]
        assert "1 | def answer():" in nodes[1]["content"]
        yield event(
            "UserMessage",
            content=[{"tool_use_id": "context", "content": reply["content"]}],
        )
        prediction = claude.native.contextbench_prediction(
            instance_id="case", messages=row["messages"], repo_path=tmp_path
        )
        assert prediction["traj_data"]["pred_spans"] == {
            "a.py": [{"start": 1, "end": 2}]
        }
        yield event(
            "AssistantMessage",
            content=[{"text": "Evidence received"}],
            model="test-model",
        )
        yield event(
            "ResultMessage",
            subtype="success",
            is_error=False,
            num_turns=2,
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
            query_text="Find answer",
            row=row,
            checkpoint=lambda: None,
            cli="test",
            model="test-model",
            max_turns=5,
            context_tool=spec,
        )
    )
    assert row["status"] == "ok" and ctx.calls
    assert row["messages"][0]["tool_calls"][0]["function"]["name"] == "explore_context"


def test_numbering_excludes_omitted_and_partial_source(tmp_path):
    (tmp_path / "a.py").write_text("one\ntwo\nthree\nfour\nfive\n")
    ctx = context(tmp_path)
    projected = {
        "verified": True,
        "start_line": 1,
        "end_line": 5,
        "content": "one\n... 2 source lines omitted ...\nfour\nfi",
    }
    assert numbered_context(ctx, "a.py", projected) == "     1 | one\n     4 | four"
    assert numbered_context(ctx, "a.py", {**projected, "verified": False}) == ""


def test_literal_omission_marker_is_source_and_changed_source_is_not_substituted(
    tmp_path,
):
    (tmp_path / "a.py").write_text("... 2 source lines omitted ...\noriginal\n")
    ctx = context(tmp_path)
    projected = {
        "verified": True,
        "start_line": 1,
        "end_line": 2,
        "content": "... 2 source lines omitted ...\noriginal\n",
    }
    assert (
        numbered_context(ctx, "a.py", projected).splitlines()[0]
        == "     1 | ... 2 source lines omitted ..."
    )
    (tmp_path / "a.py").write_text("changed\noriginal\n")
    assert numbered_context(ctx, "a.py", projected) == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("bm25", object()),
        ("grep_jev", object()),
        ("vector", None),
        ("source_verified", False),
    ],
)
def test_core_arm_rejects_missing_or_wrong_provider(tmp_path, field, value):
    ctx = context(tmp_path)
    setattr(ctx, field, value)
    with pytest.raises(ValueError):
        make_context_tool(ctx)
