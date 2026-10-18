# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Issue-only classification and actual worker dispatch; mocked models only."""

import asyncio
import copy
import json
import sys
from dataclasses import make_dataclass
from types import SimpleNamespace

import httpx
import pytest

from scripts import benchmark_context_claude as claude
from scripts.contextbench_worker_router import (
    HAIKU,
    MODEL,
    SearchWorkerRouter,
    retrieve_with_route,
)


def response(text='{"worker":"haiku","reason":"Concrete entry point"}', **extra):
    return {
        "id": "route",
        "role": "assistant",
        "model": HAIKU,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        **extra,
    }


def execute(handler, **kwargs):
    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        ) as client:
            router = SearchWorkerRouter(
                issue="Find caller behavior",
                client=client,
                checkpoint=lambda: None,
                **kwargs,
            )
            try:
                result = await router.select()
            except Exception as exc:
                return router, None, exc
            return router, result, None

    return asyncio.run(run())


@pytest.mark.parametrize("worker", ["haiku", "opus"])
def test_issue_only_route_uses_counted_exact_prompt_and_known_fees(worker):
    prompts = []

    def handler(request):
        data = json.loads(request.content)
        prompts.append(data["messages"])
        assert data["tools"] == [] and data["thinking"] == {"type": "adaptive"}
        assert json.loads(data["messages"][0]["content"]) == {
            "issue": "Find caller behavior"
        }
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        assert data["output_config"] == {"effort": "low"} and data["max_tokens"] == 768
        assert (
            data["service_tier"] == "standard_only"
            and data["inference_geo"] == "global"
        )
        return httpx.Response(
            200,
            json=response(
                json.dumps({"worker": worker, "reason": "Issue requires this worker"})
            ),
        )

    router, model, error = execute(handler)
    assert error is None and model == {"haiku": HAIKU, "opus": MODEL}[worker]
    assert len(prompts) == 2 and prompts[0] == prompts[1]
    assert router.ledger["api_usage_estimate_usd"] == pytest.approx(0.00015)
    assert (
        router.ledger["paid_message_requests"] == 1
        and router.ledger["source_credit"] == 0
    )


@pytest.mark.parametrize(
    "case", ["cap", "bad_json", "unknown_choice", "duplicate", "prose", "budget"]
)
def test_declared_known_fee_fallback_never_retries_or_dispatches_a_second_classifier(
    case,
):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(
                200, json={"input_tokens": 200000 if case == "budget" else 1000}
            )
        text = {
            "cap": '{"worker":"opus","reason":"Valid but capped is never trusted"}',
            "bad_json": "broken",
            "unknown_choice": '{"worker":"other","reason":"Unsupported"}',
            "duplicate": '{"worker":"opus","worker":"haiku","reason":"Conflicting"}',
            "prose": 'Here is the route: {"worker":"opus","reason":"No extraction"}',
        }[case]
        return httpx.Response(
            200,
            json=response(
                text, stop_reason="max_tokens" if case == "cap" else "end_turn"
            ),
        )

    router, model, error = execute(handler)
    assert error is None and model == HAIKU and router.ledger["failed"] is False
    assert len(calls) == (1 if case == "budget" else 2)
    assert router.ledger["api_usage_estimate_usd"] == pytest.approx(
        0 if case == "budget" else 0.00015
    )
    assert router.ledger["status"] in [
        "budget_deferred_haiku",
        "capped_haiku",
        "malformed_haiku",
    ]


@pytest.mark.parametrize(
    "failure",
    ["model", "usage", "cache", "http", "content", "output_cap", "actual_budget"],
)
def test_integrity_failure_keeps_known_or_unknown_fee_and_selects_no_worker(failure):
    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        data = response()
        if failure == "model":
            data["model"] = "unknown"
        if failure == "usage":
            data["usage"] = {}
        if failure == "cache":
            data["usage"]["cache_read_input_tokens"] = 100
        if failure == "http":
            return httpx.Response(503, json={"error": "unavailable"})
        if failure == "content":
            data["content"] = [{"type": "tool_use", "name": "search"}]
        if failure == "output_cap":
            data["usage"]["output_tokens"] = 769
        if failure == "actual_budget":
            data["usage"]["input_tokens"] = 200000
        return httpx.Response(200, json=data)

    router, model, error = execute(handler)
    assert error is not None and model is None and router.ledger["failed"]
    assert "selected_worker" not in router.ledger
    if failure in {"model", "usage", "cache", "http"}:
        assert router.ledger["api_usage_estimate_usd"] is None
    else:
        assert router.ledger["api_usage_estimate_usd"] > 0
    assert router.ledger["paid_message_requests"] == 1


@pytest.mark.parametrize("failure", ["unknown_model", "invalid_response"])
def test_route_integrity_failure_never_dispatches_worker_or_hides_its_own_fee(
    tmp_path, monkeypatch, failure
):
    async def forbidden(**kwargs):
        raise AssertionError("Worker must not dispatch")

    monkeypatch.setattr(claude, "retrieve", forbidden)
    row = {}

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        data = response()
        if failure == "unknown_model":
            data["model"] = "unexpected"
        else:
            data["content"] = [{"type": "tool_use", "name": "unexpected"}]
        return httpx.Response(200, json=data)

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        ) as client:
            router = SearchWorkerRouter(
                issue="Find caller", client=client, checkpoint=lambda: None
            )
            with pytest.raises(ValueError):
                await retrieve_with_route(
                    router=router,
                    repo=tmp_path,
                    query_text="Find caller",
                    row=row,
                    checkpoint=lambda: None,
                    cli="test",
                    worker_budgets={HAIKU: 0.08, MODEL: 0.5},
                )
            assert row["worker_query_dispatched"] is False
            assert row["all_model_usage_estimate_usd"] == (
                None if failure == "unknown_model" else pytest.approx(0.00015)
            )

    asyncio.run(run())


def test_dispatched_worker_without_terminal_keeps_worker_fee_unknown(
    tmp_path, monkeypatch
):
    async def worker(**kwargs):
        raise RuntimeError("No terminal receipt")

    monkeypatch.setattr(claude, "retrieve", worker)
    row = {}

    def handler(request):
        return httpx.Response(
            200,
            json=(
                {"input_tokens": 1000}
                if request.url.path.endswith("count_tokens")
                else response()
            ),
        )

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        ) as client:
            router = SearchWorkerRouter(
                issue="Find caller", client=client, checkpoint=lambda: None
            )
            with pytest.raises(RuntimeError, match="No terminal"):
                await retrieve_with_route(
                    router=router,
                    repo=tmp_path,
                    query_text="Find caller",
                    row=row,
                    checkpoint=lambda: None,
                    cli="test",
                    worker_budgets={HAIKU: 0.08, MODEL: 0.5},
                )
            assert row["worker_query_dispatched"] is True
            assert row["worker_router"]["api_usage_estimate_usd"] == pytest.approx(
                0.00015
            )
            assert row["all_model_usage_estimate_usd"] is None

    asyncio.run(run())


def test_one_attempt_latch_rejects_concurrent_and_repeated_selection():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("count_tokens"):
                entered.set()
                await release.wait()
                return httpx.Response(200, json={"input_tokens": 1000})
            return httpx.Response(200, json=response())

        async with httpx.AsyncClient(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        ) as client:
            router = SearchWorkerRouter(
                issue="Find caller", client=client, checkpoint=lambda: None
            )
            first = asyncio.create_task(router.select())
            await entered.wait()
            with pytest.raises(RuntimeError, match="already attempted"):
                await router.select()
            release.set()
            assert await first == HAIKU
            with pytest.raises(RuntimeError, match="already attempted"):
                await router.select()
            assert len(calls) == 2 and router.ledger["paid_message_requests"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("worker", ["haiku", "opus", "malformed"])
@pytest.mark.parametrize("worker_error", [False, True])
def test_actual_selected_worker_keeps_original_prompt_source_and_both_fees(
    tmp_path, monkeypatch, worker, worker_error
):
    (tmp_path / "a.py").write_text("def caller():\n    return 1\n")
    row = {
        "messages": [{"role": "user", "content": "Find caller behavior"}],
        "sdk_events": [],
    }
    saved = []
    selected = MODEL if worker == "opus" else HAIKU

    def event(kind, **fields):
        return make_dataclass(kind, [(k, object) for k in fields])(**fields)

    async def query(*, prompt, options):
        assert prompt == "Find caller behavior"
        assert (
            options.model == selected
            and options.effort == "medium"
            and options.max_turns == 20
        )
        assert (
            options.agents == {}
            and options.tools == []
            and not getattr(options, "hooks", None)
        )
        assert set(options.allowed_tools) == set(claude.TOOLS)
        assert options.max_budget_usd == (0.08 if selected == HAIKU else 0.5)
        handlers = dict(options.mcp_servers["source"])
        yield event(
            "SystemMessage",
            subtype="init",
            data={"apiKeySource": "ANTHROPIC_API_KEY", "tools": list(claude.TOOLS)},
        )
        yield event(
            "AssistantMessage",
            content=[
                {
                    "id": "read",
                    "name": "mcp__source__read",
                    "input": {"file_path": "a.py", "limit": 2},
                }
            ],
            model=selected,
        )
        reply = await handlers["read"]({"file_path": "a.py", "limit": 2})
        yield event(
            "UserMessage",
            content=[{"tool_use_id": "read", "content": reply["content"]}],
        )
        yield event("AssistantMessage", content=[{"text": "Done"}], model=selected)
        yield event(
            "ResultMessage",
            subtype="success",
            is_error=False,
            num_turns=3,
            total_cost_usd=0.003,
            result="Done",
        )
        if worker_error:
            raise RuntimeError("Post-terminal SDK transport failure")

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

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1000})
        text = (
            "bad decision"
            if worker == "malformed"
            else json.dumps({"worker": worker, "reason": "ROUTER_METADATA_ONLY"})
        )
        return httpx.Response(200, json=response(text))

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
        ) as client:
            router = SearchWorkerRouter(
                issue="Find caller behavior",
                client=client,
                checkpoint=lambda: saved.append(copy.deepcopy(row)),
            )
            args = dict(
                router=router,
                repo=tmp_path,
                query_text="Find caller behavior",
                row=row,
                checkpoint=lambda: saved.append(copy.deepcopy(row)),
                cli="test",
                worker_budgets={HAIKU: 0.08, MODEL: 0.5},
            )
            if worker_error:
                with pytest.raises(RuntimeError, match="Post-terminal"):
                    await retrieve_with_route(**args)
            else:
                await retrieve_with_route(**args)
                prediction = claude.native.contextbench_prediction(
                    instance_id="case", messages=row["messages"], repo_path=tmp_path
                )
                assert prediction["traj_data"]["pred_spans"] == {
                    "a.py": [{"start": 1, "end": 2}]
                }
            assert row["selected_worker_model"] == selected
            assert row["sdk_reported_cost_usd"] == 0.003 and row[
                "all_model_usage_estimate_usd"
            ] == pytest.approx(0.00315)
            assert "ROUTER_METADATA_ONLY" not in json.dumps(row["messages"])
            before = copy.deepcopy(row)
            with pytest.raises(ValueError, match="must match"):
                await retrieve_with_route(**args)
            assert row == before

    asyncio.run(run())
    assert saved[-1]["all_model_usage_estimate_usd"] == pytest.approx(0.00315)
