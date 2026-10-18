# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Same-session evidence routing and metering with real source, mocked models."""

import asyncio
import copy
import json
import sys
from dataclasses import make_dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from codenib.agent.tools.defaults import _read
from codenib.compiler.manifest import RepoManifest
from codenib.mcp.context import ServerContext
from codenib.source_fingerprint import capture_repository_source, fingerprint_repository
from scripts import benchmark_context_claude as claude
from scripts.contextbench_evidence_review import EvidenceReviewGuard
from scripts.contextbench_packet_reader import API_URL, MODEL


@pytest.fixture
def source(tmp_path):
    (tmp_path / "a.py").write_text(
        "def caller():\n    return helper()\n\ndef helper():\n    return 'UNREAD_BODY'\n"
    )
    fingerprint = fingerprint_repository(tmp_path)
    ctx = ServerContext.load(
        RepoManifest(
            repo_path=str(tmp_path),
            commit="a" * 40,
            source_fingerprint=fingerprint.value,
            file_count=fingerprint.file_count,
        ),
        views=[],
        source_binding=capture_repository_source(tmp_path),
    )
    try:
        yield ctx
    finally:
        ctx.close()


def exchange(ctx, *, offset=1, limit=2, call_id="read"):
    args = {"file_path": "a.py", "offset": offset, "limit": limit}
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
            "content": _read(str(Path(ctx.manifest.repo_path) / "a.py"), offset, limit),
        },
        {
            "role": "assistant",
            "content": "The caller delegates; implementation unresolved.",
        },
    ]


def response(text, **extra):
    return {
        "id": "review",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        **extra,
    }


def continue_decision(packet):
    return json.dumps(
        {
            "decision": "continue",
            "gaps": [
                {
                    "why": "The called helper's behavior remains unverified.",
                    "evidence": [packet["evidence"][0]["handle"]],
                    "question": "Where is helper defined and what does it return?",
                }
            ],
        }
    )


def stop_data(ctx, **overrides):
    return {
        "hook_event_name": "Stop",
        "cwd": ctx.manifest.repo_path,
        "stop_hook_active": False,
        "session_id": "search-session",
        **overrides,
    }


def execute(ctx, handler, *, history=None, **kwargs):
    async def run():
        async with httpx.AsyncClient(
            base_url=API_URL, transport=httpx.MockTransport(handler)
        ) as client:
            guard = EvidenceReviewGuard(
                ctx,
                issue="Explain the helper return value",
                observed_messages=lambda: exchange(ctx) if history is None else history,
                client=client,
                checkpoint=lambda: None,
                **kwargs,
            )
            guard.bind_session("search-session")
            outcome = await guard.stop(stop_data(ctx), None, {})
            return guard, outcome

    return asyncio.run(run())


def test_packet_deduplicates_only_confirmed_complete_lines_and_keeps_search_metadata(
    source,
):
    history = exchange(source)
    history.extend(exchange(source, call_id="duplicate"))
    history.extend(exchange(source, offset=4, limit=2, call_id="unconfirmed")[:-1])
    # A later incomplete/fabricated line must not be restored from the file.
    history[1]["content"] += "\n5 | fabricated ... [line truncated]"
    calls = []

    def handler(request):
        calls.append(request.url.path)
        payload = json.loads(request.content)
        packet = json.loads(payload["messages"][0]["content"])
        assert packet["handoff"] == "The caller delegates; implementation unresolved."
        assert len(packet["evidence"]) == 1
        assert packet["evidence"][0]["lines"] == [1, 2]
        assert "UNREAD_BODY" not in payload["messages"][0]["content"]
        assert len(packet["search_history"]) == 2
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        return httpx.Response(200, json=response(continue_decision(packet)))

    guard, outcome = execute(source, handler, history=history)
    assert outcome.get("decision") == "block", guard.ledger
    assert guard.ledger["input_confirmed"]
    assert guard.ledger["new_source_lines_from_review"] == 0
    assert guard.ledger["api_usage_estimate_usd"] == pytest.approx(0.006)
    assert calls == ["/v1/messages/count_tokens", "/v1/messages"]
    assert "UNREAD_BODY" not in outcome["reason"]


@pytest.mark.parametrize(
    "policy", ["budget", "packet", "no_evidence", "capped", "stop"]
)
def test_declared_no_action_outcomes_retain_native_search_and_explicit_fees(
    source, policy
):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(
                200, json={"input_tokens": 100000 if policy == "budget" else 1000}
            )
        return httpx.Response(
            200,
            json=response(
                (
                    '{"decision":'
                    if policy == "capped"
                    else '{"decision":"stop","gaps":[]}'
                ),
                stop_reason="max_tokens" if policy == "capped" else "end_turn",
            ),
        )

    kwargs = {"max_packet_bytes": 10} if policy == "packet" else {}
    guard, outcome = execute(
        source, handler, history=[] if policy == "no_evidence" else None, **kwargs
    )
    assert outcome == {} and not guard.ledger["failed"]
    assert (
        guard.ledger["status"]
        == {
            "budget": "deferred_budget",
            "packet": "deferred_packet_limit",
            "no_evidence": "deferred_no_verified_evidence",
            "capped": "capped_no_action",
            "stop": "reviewed",
        }[policy]
    )
    if policy in {"capped", "stop"}:
        assert guard.ledger["paid_message_requests"] == 1
        assert guard.ledger["api_usage_estimate_usd"] == pytest.approx(0.006)
    else:
        assert guard.ledger["paid_message_requests"] == 0
        assert guard.ledger["api_usage_estimate_usd"] == 0
        assert not guard.ledger["input_confirmed"]
    assert len(calls) == (
        1 if policy == "budget" else 0 if policy in {"packet", "no_evidence"} else 2
    )


@pytest.mark.parametrize(
    "failure",
    [
        "before",
        "after_count",
        "after_paid",
        "model",
        "usage",
        "http",
        "json",
        "handle",
        "duplicate_key",
    ],
)
def test_failed_review_preserves_dispatch_and_known_or_unknown_costs(source, failure):
    def mutate():
        (Path(source.manifest.repo_path) / "a.py").write_text("changed\n")

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            if failure == "after_count":
                mutate()
            return httpx.Response(200, json={"input_tokens": 1000})
        packet = json.loads(json.loads(request.content)["messages"][0]["content"])
        data = response(continue_decision(packet))
        if failure == "after_paid":
            mutate()
        elif failure == "model":
            data["model"] = "unexpected"
        elif failure == "usage":
            data["usage"] = {}
        elif failure == "http":
            return httpx.Response(503, json={"error": "unavailable"})
        elif failure == "json":
            data["content"][0]["text"] = "broken"
        elif failure == "handle":
            data["content"][0]["text"] = continue_decision(packet).replace(
                packet["evidence"][0]["handle"], "unknown"
            )
        elif failure == "duplicate_key":
            data["content"][0][
                "text"
            ] = '{"decision":"continue","decision":"stop","gaps":[]}'
        return httpx.Response(200, json=data)

    history = exchange(source)
    if failure == "before":
        mutate()
    guard, outcome = execute(source, handler, history=history)
    assert outcome == {} and guard.ledger["failed"]
    assert guard.ledger["paid_message_requests"] == (
        0 if failure in {"before", "after_count"} else 1
    )
    fee = guard.ledger["api_usage_estimate_usd"]
    if failure in {"model", "usage", "http"}:
        assert fee is None
    else:
        assert fee == pytest.approx(
            0 if failure in {"before", "after_count"} else 0.006
        )


def test_concurrent_stop_callbacks_dispatch_once_and_network_holds_no_source_lease(
    source,
):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(request):
            # A different thread can acquire the view lock during HTTP. Holding
            # either view/reader lock would block this assertion or source read.
            def read_from_other_thread():
                with source._view_lock:
                    return source.borrow_source_reader().read_prefix(
                        "a.py", max_bytes=1000
                    )

            assert b"caller" in await asyncio.wait_for(
                asyncio.to_thread(read_from_other_thread), timeout=2
            )
            calls.append(request.url.path)
            if request.url.path.endswith("count_tokens"):
                entered.set()
                await release.wait()
                return httpx.Response(200, json={"input_tokens": 1000})
            return httpx.Response(200, json=response('{"decision":"stop","gaps":[]}'))

        async with httpx.AsyncClient(
            base_url=API_URL, transport=httpx.MockTransport(handler)
        ) as client:
            guard = EvidenceReviewGuard(
                source,
                issue="Find helper",
                observed_messages=lambda: exchange(source),
                client=client,
                checkpoint=lambda: None,
            )
            guard.bind_session("search-session")
            first = asyncio.create_task(guard.stop(stop_data(source), None, {}))
            await entered.wait()
            assert await guard.stop(stop_data(source), None, {}) == {}
            release.set()
            assert await first == {}
            assert (
                await guard.stop(stop_data(source, stop_hook_active=True), None, {})
                == {}
            )
            assert guard.ledger["paid_message_requests"] == 1
            assert len(calls) == 2 and not guard.ledger["failed"]

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", ["repository", "session", "missing_init"])
def test_hook_identity_mismatch_never_dispatches(source, mismatch):
    async def run():
        async with httpx.AsyncClient(
            base_url=API_URL,
            transport=httpx.MockTransport(
                lambda request: pytest.fail("Unexpected HTTP")
            ),
        ) as client:
            guard = EvidenceReviewGuard(
                source,
                issue="Find helper",
                observed_messages=lambda: exchange(source),
                client=client,
                checkpoint=lambda: None,
            )
            if mismatch != "missing_init":
                guard.bind_session("search-session")
            data = stop_data(source)
            if mismatch == "repository":
                data["cwd"] = str(Path(source.manifest.repo_path).parent)
            elif mismatch == "session":
                data["session_id"] = "other-session"
            assert await guard.stop(data, None, {}) == {}
            assert guard.ledger["failed"] and guard.ledger["paid_message_requests"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("bad_review", [False, "json", "model", "after_continuation"])
def test_sdk_same_session_continuation_or_failed_review_drains_both_fees(
    source, monkeypatch, bad_review
):
    row = {"messages": [], "sdk_events": []}
    saved = []

    def event(kind, **fields):
        return make_dataclass(kind, [(key, object) for key in fields])(**fields)

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        packet = json.loads(json.loads(request.content)["messages"][0]["content"])
        data = response(
            "bad JSON" if bad_review == "json" else continue_decision(packet)
        )
        if bad_review == "model":
            data["model"] = "unknown-model"
        return httpx.Response(200, json=data)

    async def query(*, prompt, options):
        assert set(options.allowed_tools) == set(claude.TOOLS)
        assert options.agents == {} and options.tools == []
        assert options.hooks["Stop"][0].timeout == 180
        callback = options.hooks["Stop"][0].hooks[0]
        handlers = dict(options.mcp_servers["source"])
        yield event(
            "SystemMessage",
            subtype="init",
            data={
                "apiKeySource": "ANTHROPIC_API_KEY",
                "tools": list(claude.TOOLS),
                "session_id": "search-session",
            },
        )
        for call_id, offset, limit in [("first", 1, 2), ("helper", 4, 2)]:
            args = {"file_path": "a.py", "offset": offset, "limit": limit}
            yield event(
                "AssistantMessage",
                content=[{"id": call_id, "name": "mcp__source__read", "input": args}],
                model="search-model",
            )
            result = await handlers["read"](args)
            yield event(
                "UserMessage",
                content=[{"tool_use_id": call_id, "content": result["content"]}],
            )
            yield event(
                "AssistantMessage", content=[{"text": "Handoff"}], model="search-model"
            )
            outcome = await callback(stop_data(source), None, {})
            if bad_review in {"json", "model"}:
                assert outcome == {}
                break
            if call_id == "first":
                assert outcome["decision"] == "block"
                assert "UNREAD_BODY" not in json.dumps(row["evidence_review"]["packet"])
            else:
                assert outcome == {}
        if bad_review == "after_continuation":
            (Path(source.manifest.repo_path) / "a.py").write_text(
                "changed after search\n"
            )
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

    async def run():
        async with httpx.AsyncClient(
            base_url=API_URL, transport=httpx.MockTransport(handler)
        ) as client:
            guard = EvidenceReviewGuard(
                source,
                issue="Find helper",
                observed_messages=lambda: row["messages"],
                client=client,
                checkpoint=lambda: saved.append(copy.deepcopy(row)),
            )
            kwargs = dict(
                repo=Path(source.manifest.repo_path),
                query_text="Find helper",
                row=row,
                checkpoint=lambda: saved.append(copy.deepcopy(row)),
                cli="test",
                model="search-model",
                max_turns=5,
                billing_route="anthropic-api",
                evidence_reviewer=guard,
            )
            if bad_review:
                with pytest.raises(RuntimeError, match="both fee receipts retained"):
                    await claude.retrieve(**kwargs)
            else:
                await claude.retrieve(**kwargs)
                assert row["status"] == "ok"
                assert "UNREAD_BODY" in json.dumps(row["messages"])
            assert row["sdk_reported_cost_usd"] == 0.003
            if bad_review == "model":
                assert row["evidence_review"]["api_usage_estimate_usd"] is None
                assert row["all_model_usage_estimate_usd"] is None
            else:
                assert row["evidence_review"][
                    "api_usage_estimate_usd"
                ] == pytest.approx(0.006)
                assert row["all_model_usage_estimate_usd"] == pytest.approx(0.009)
            assert row["evidence_review"]["paid_message_requests"] == 1

    asyncio.run(run())
    assert any(item.get("evidence_review", {}).get("response") for item in saved)
