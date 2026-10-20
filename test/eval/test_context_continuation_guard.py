# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Authenticated callable closure, bounded intervention and SDK cost retention."""

import asyncio
import json
import sys
from dataclasses import make_dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.agent.tools.defaults import _read
from codenib.compiler.manifest import RepoManifest
from codenib.mcp.context import ServerContext
from codenib.source_fingerprint import capture_repository_source, fingerprint_repository
from scripts import benchmark_context_claude as claude
from scripts.contextbench_continuation_guard import ContinuationGuard


@pytest.fixture
def source(tmp_path):
    # A native 200-line page ends inside the 179--220 callable. Its sibling
    # starts at 222 and must never be included in a closure recommendation.
    (tmp_path / "a.go").write_text(
        "package p\n"
        + "// before\n" * 177
        + "func ResolveWorkflow() {\n"
        + '    println("UNREAD_TAIL")\n' * 40
        + '}\n\nfunc Sibling() { println("UNRELATED_BODY") }\n'
    )
    fp = fingerprint_repository(tmp_path)
    ctx = ServerContext.load(
        RepoManifest(
            repo_path=str(tmp_path),
            commit="a" * 40,
            source_fingerprint=fp.value,
            file_count=fp.file_count,
        ),
        views=[],
        source_binding=capture_repository_source(tmp_path),
    )
    try:
        yield ctx
    finally:
        ctx.vector = None
        ctx.close()


def exchange(ctx, *, offset=1, limit=200, call_id="read"):
    args = {"file_path": "a.go", "offset": offset, "limit": limit}
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {"name": "read", "arguments": json.dumps(args)},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _read(str(Path(ctx.manifest.repo_path) / "a.go"), offset, limit),
        },
        {"role": "assistant", "content": "Evidence received"},
    ]


def stop(guard, **extra):
    return asyncio.run(
        guard.stop(
            {
                "hook_event_name": "Stop",
                "cwd": guard.ctx.manifest.repo_path,
                "stop_hook_active": False,
                **extra,
            },
            None,
            {},
        )
    )


def predict(ctx, history):
    return claude.native.contextbench_prediction(
        instance_id="case", messages=history, repo_path=ctx.manifest.repo_path
    )["traj_data"]["pred_spans"]


def test_closure_is_metadata_only_bounded_to_callable_and_at_most_once(source):
    history = exchange(source)
    guard = ContinuationGuard(source, observed_messages=lambda: history)
    before = predict(source, history)
    result = stop(guard)
    assert result["decision"] == "block"
    candidate = guard.ledger["stop_events"][0]["candidate"]
    assert candidate["function_lines"] == [179, 220]
    assert candidate["read_next"] == {"file_path": "a.go", "offset": 201, "limit": 20}
    assert candidate["unseen_tail_lines"] == 20
    assert "UNREAD_TAIL" not in result["reason"]
    assert "UNRELATED_BODY" not in result["reason"]
    assert predict(source, history) == before == {"a.go": [{"start": 1, "end": 200}]}
    assert stop(guard) == {}  # even a false active flag cannot trigger twice


@pytest.mark.parametrize(
    "condition",
    ["unconfirmed", "fully_seen", "at_boundary", "no_signature", "too_long", "partial"],
)
def test_unsupported_or_already_closed_reads_do_not_force_source(source, condition):
    history = exchange(source)
    options = {}
    if condition == "unconfirmed":
        history.pop()
    elif condition == "fully_seen":
        history.extend(exchange(source, offset=201, limit=20, call_id="tail"))
    elif condition == "at_boundary":
        history = exchange(source, limit=220)
    elif condition == "no_signature":
        history = exchange(source, offset=190, limit=11)
    elif condition == "too_long":
        options["max_tail_lines"] = 19
    else:
        history[1]["content"] = history[1]["content"].replace(
            '200 |     println("UNREAD_TAIL")',
            '200 |     println("UNREAD_TAIL") ... [line truncated]',
        )
    guard = ContinuationGuard(source, observed_messages=lambda: history, **options)
    assert stop(guard) == {}
    assert not guard.ledger["failed"] and not guard.intervened


@pytest.mark.parametrize("active", [True, None, "false"])
def test_noninitial_stop_never_loops(source, active):
    guard = ContinuationGuard(source, observed_messages=lambda: exchange(source))
    assert stop(guard, stop_hook_active=active) == {}


def test_oversized_source_is_an_explicit_coverage_gap(source, monkeypatch):
    monkeypatch.setattr(
        "scripts.contextbench_continuation_guard._MAX_SOURCE_BYTES", 100
    )
    guard = ContinuationGuard(source, observed_messages=lambda: exchange(source))
    assert stop(guard) == {}
    assert guard.ledger["diagnostics"] == [
        {"file": "a.go", "coverage": "source_byte_limit"}
    ]
    assert not guard.ledger["failed"]


def test_guard_rejects_indexed_context_or_changed_snapshot(source):
    source.vector = SimpleNamespace()
    with pytest.raises(ValueError, match="authenticated source only"):
        ContinuationGuard(source, observed_messages=lambda: [])
    source.vector = None
    guard = ContinuationGuard(source, observed_messages=lambda: exchange(source))
    source.manifest.commit = "b" * 40
    assert stop(guard) == {}
    assert guard.ledger["failed"] and not guard.intervened


def test_guard_cannot_be_combined_with_another_enhanced_arm(source, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            ClaudeAgentOptions=None, create_sdk_mcp_server=None, query=None, tool=None
        ),
    )
    guard = ContinuationGuard(source, observed_messages=lambda: [])
    with pytest.raises(ValueError, match="otherwise native source tools"):
        asyncio.run(
            claude.retrieve(
                repo=Path(source.manifest.repo_path),
                query_text="Find workflow",
                row={},
                checkpoint=lambda: None,
                cli="test",
                model="test-model",
                max_turns=5,
                file_map_tokens=1024,
                continuation_guard=guard,
            )
        )


@pytest.mark.parametrize("stale", [False, True])
def test_sdk_guard_delivers_real_tail_or_drains_failed_source_cost(
    source, monkeypatch, stale
):
    repo = Path(source.manifest.repo_path)
    row = {"messages": [], "sdk_events": []}
    guard = ContinuationGuard(source, observed_messages=lambda: row["messages"])

    def event(kind, **fields):
        return make_dataclass(kind, [(k, object) for k in fields])(**fields)

    async def query(*, prompt, options):
        assert set(options.allowed_tools) == set(claude.TOOLS)
        assert options.tools == [] and options.agents == {}
        callback = options.hooks["Stop"][0].hooks[0]
        handlers = dict(options.mcp_servers["source"])
        yield event(
            "SystemMessage",
            subtype="init",
            data={"apiKeySource": "ANTHROPIC_API_KEY", "tools": list(claude.TOOLS)},
        )
        for call_id, args in [
            ("first", {"file_path": "a.go", "limit": 200}),
            ("tail", {"file_path": "a.go", "offset": 201, "limit": 20}),
        ]:
            yield event(
                "AssistantMessage",
                content=[{"id": call_id, "name": "mcp__source__read", "input": args}],
                model="test-model",
            )
            result = await handlers["read"](args)
            assert not result["isError"]
            yield event(
                "UserMessage",
                content=[{"tool_use_id": call_id, "content": result["content"]}],
            )
            yield event(
                "AssistantMessage", content=[{"text": "Done"}], model="test-model"
            )
            if stale:
                (repo / "a.go").write_text("package changed\n")
            hook_result = await callback(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(repo),
                    "stop_hook_active": False,
                },
                None,
                {},
            )
            if stale:
                assert hook_result == {} and guard.ledger["failed"]
                break
            if call_id == "first":
                assert hook_result["decision"] == "block"
                assert predict(source, row["messages"])["a.go"][-1]["end"] == 200
            else:
                assert hook_result == {}
        yield event(
            "ResultMessage",
            subtype="success",
            is_error=False,
            num_turns=4,
            total_cost_usd=0.003,
            result="Done",
        )

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            ClaudeAgentOptions=SimpleNamespace,
            HookMatcher=SimpleNamespace,
            create_sdk_mcp_server=lambda name, tools: tools,
            tool=lambda name, description, schema: lambda handler: (name, handler),
            query=query,
        ),
    )
    kwargs = dict(
        repo=repo,
        query_text="Locate workflow selection",
        row=row,
        checkpoint=lambda: None,
        cli="test",
        model="test-model",
        max_turns=5,
        billing_route="anthropic-api",
        continuation_guard=guard,
    )
    if stale:
        with pytest.raises(RuntimeError, match="Continuation guard source inspection"):
            asyncio.run(claude.retrieve(**kwargs))
        assert row["sdk_result"]["total_cost_usd"] == 0.003
    else:
        asyncio.run(claude.retrieve(**kwargs))
        assert row["status"] == "ok"
        assert predict(source, row["messages"]) == {"a.go": [{"start": 1, "end": 220}]}
    assert row["sdk_reported_cost_usd"] == 0.003
    assert row["continuation_guard"] is guard.ledger
