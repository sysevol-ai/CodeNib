"""Subscription benchmark boundaries; no SDK installation or live calls required."""

import asyncio
import json
import sys
from dataclasses import make_dataclass
from types import SimpleNamespace

import pytest

from scripts import benchmark_context_claude as claude


def test_sdk_tool_results_count_returned_lines_only(tmp_path):
    (tmp_path / "a.py").write_text("visible\nunseen\n")
    messages = claude.normalize_event(
        "AssistantMessage",
        {
            "content": [
                {
                    "id": "read-id",
                    "name": "mcp__source__read",
                    "input": {"file_path": "a.py", "offset": 1, "limit": 200},
                }
            ]
        },
    )
    messages += claude.normalize_event(
        "UserMessage",
        {
            "content": [
                {
                    "tool_use_id": "read-id",
                    "content": [{"type": "text", "text": "     1 | visible"}],
                }
            ]
        },
    )
    prediction = claude.native.contextbench_prediction(
        instance_id="case", messages=messages, repo_path=tmp_path
    )
    assert prediction["traj_data"]["pred_spans"] == {"a.py": [{"start": 1, "end": 1}]}
    attempted = claude.normalize_event(
        "AssistantMessage",
        {"content": [{"id": "unexpected", "name": "Bash", "input": {}}]},
    )
    assert attempted[0]["tool_calls"][0]["function"] == {
        "name": "Bash",
        "arguments": "{}",
        "unavailable": True,
    }


def test_subscription_auth_refuses_api_route_and_omits_identity(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-use")
    with pytest.raises(ValueError, match="existing CLI login only"):
        claude.subscription_auth("claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    for key in (
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        monkeypatch.delenv(key, raising=False)

    class Auth:
        stdout = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
                "email": "private@example.org",
            }
        )

    monkeypatch.setattr(claude.subprocess, "run", lambda *a, **kw: Auth())
    status = claude.subscription_auth("claude")
    assert status["subscriptionType"] == "max" and "email" not in status


def test_graph_sdk_tool_requires_explicit_run_allowlist():
    event = {
        "content": [
            {
                "id": "graph",
                "name": "mcp__source__graph_neighbors",
                "input": {"file_path": "a.py", "line": 1},
            }
        ]
    }
    unavailable = claude.normalize_event("AssistantMessage", event)
    assert unavailable[0]["tool_calls"][0]["function"]["unavailable"]
    normalized = claude.normalize_event(
        "AssistantMessage",
        event,
        {**claude.TOOLS, "mcp__source__graph_neighbors": "graph_neighbors"},
    )
    assert normalized[0]["tool_calls"][0]["function"]["name"] == "graph_neighbors"
    assert "unavailable" not in normalized[0]["tool_calls"][0]["function"]


@pytest.mark.parametrize("policy", ["definitions", "unseen"])
@pytest.mark.parametrize("query_policy", [None, *claude.GRAPH_QUERY_POLICIES])
def test_sdk_completion_uses_streamed_source_receipts(
    monkeypatch, tmp_path, policy, query_policy
):
    from scripts.contextbench_graph_navigation import GraphNavigation

    (tmp_path / "a.py").write_text("def found():\n    value = 3\n    return value\n")
    row = {"instance_id": "case", "messages": [], "sdk_events": []}
    replies = []

    def event(kind, **fields):
        return make_dataclass(kind, [(key, object) for key in fields])(**fields)

    async def query(*, prompt, options):
        handlers = dict(options.mcp_servers["source"])
        assert ("graph_neighbors" in handlers) == (query_policy is not None)
        assert ("graph_neighbors" in options.system_prompt) == (
            query_policy is not None
        )
        if query_policy is not None:
            early = query_policy == claude.GRAPH_QUERY_POLICIES[0]
            assert (claude.native.GRAPH_INSTRUCTIONS in options.system_prompt) == early
            assert (claude.native.LATE_GRAPH_INSTRUCTIONS in options.system_prompt) == (
                not early
            )
        yield event(
            "SystemMessage", subtype="init", data={"tools": options.allowed_tools}
        )
        for offset in (1, 2):
            arguments = {"file_path": "a.py", "offset": offset, "limit": 1}
            identifier = str(offset)
            yield event(
                "AssistantMessage",
                content=[
                    {
                        "id": identifier,
                        "name": "mcp__source__read",
                        "input": arguments,
                    }
                ],
                model="test-model",
            )
            reply = await handlers["read"](arguments)
            assert not reply["isError"]
            replies.append(reply["content"][0]["text"])
            yield event(
                "UserMessage",
                content=[{"tool_use_id": identifier, "content": reply["content"]}],
            )
        yield event("AssistantMessage", content=[{"text": "Done"}], model="test-model")
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
            tool=lambda name, *args: lambda handler: (name, handler),
        ),
    )
    asyncio.run(
        claude.retrieve(
            repo=tmp_path,
            query_text="Inspect found",
            row=row,
            checkpoint=lambda: None,
            cli="test",
            model="test-model",
            max_turns=5,
            source_policy="functions",
            completion_source_policy=policy,
            graph_tool=(
                GraphNavigation(None, tmp_path).tool_spec() if query_policy else None
            ),
            graph_query_policy=query_policy or claude.GRAPH_QUERY_POLICIES[0],
        )
    )
    assert "return value" in replies[0] and "value = 3" in replies[1]
    assert ("return value" in replies[1]) == (policy == "definitions")
    assert row["status"] == "ok" and row["sdk_reported_cost_usd"] == 0.03
    assert claude.native.contextbench_prediction(
        instance_id="case", messages=row["messages"], repo_path=tmp_path
    )["traj_data"]["pred_spans"] == {"a.py": [{"start": 1, "end": 3}]}


def test_sdk_terminal_meter_is_checkpointed_before_iterator_error(
    monkeypatch, tmp_path
):
    row = {"messages": [], "sdk_events": []}
    checkpoints = []
    result_type = make_dataclass(
        "ResultMessage",
        [(key, object) for key in ("subtype", "is_error", "total_cost_usd")],
    )

    async def query(**kwargs):
        yield result_type("error_max_budget_usd", True, 0.4)
        assert checkpoints[-1]["sdk_reported_cost_usd"] == 0.4
        raise RuntimeError("SDK process failed after terminal result")

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            ClaudeAgentOptions=SimpleNamespace,
            create_sdk_mcp_server=lambda name, tools: tools,
            query=query,
            tool=lambda *args: lambda handler: handler,
        ),
    )
    with pytest.raises(RuntimeError, match="failed after terminal result"):
        asyncio.run(
            claude.retrieve(
                repo=tmp_path,
                query_text="issue",
                row=row,
                checkpoint=lambda: checkpoints.append(json.loads(json.dumps(row))),
                cli="test",
                model="test-model",
                max_turns=5,
            )
        )
    assert row["sdk_reported_cost_usd"] == 0.4
    assert row["sdk_result"]["is_error"]
    assert row.get("status") != "ok"


@pytest.mark.parametrize("unexpected_inventory", [False, True])
def test_sdk_unknown_attempt_retains_usage_but_rejects_extra_capability(
    monkeypatch, tmp_path, unexpected_inventory
):
    (tmp_path / "a.py").write_text("visible\nunseen\n")
    row = {"instance_id": "case", "messages": [], "sdk_events": []}

    def event(kind, **fields):
        return make_dataclass(kind, [(key, object) for key in fields])(**fields)

    rejected = json.dumps([{"file": "a.py", "content": "2 | unseen"}])

    async def query(*, prompt, options):
        assert options.tools == [] and options.strict_mcp_config
        assert set(options.allowed_tools) == set(claude.TOOLS)
        assert "graph_neighbors" not in options.system_prompt
        yield event(
            "SystemMessage",
            subtype="init",
            data={
                "tools": options.allowed_tools
                + (["Bash"] if unexpected_inventory else [])
            },
        )
        yield event(
            "AssistantMessage",
            content=[
                {"id": "absent", "name": "mcp__source__graph_neighbors", "input": {}}
            ],
            model="test-model",
        )
        yield event(
            "UserMessage",
            content=[{"tool_use_id": "absent", "content": rejected, "is_error": True}],
        )
        yield event(
            "AssistantMessage",
            content=[
                {
                    "id": "read",
                    "name": "mcp__source__read",
                    "input": {"file_path": "a.py"},
                }
            ],
            model="test-model",
        )
        yield event(
            "UserMessage",
            content=[{"tool_use_id": "read", "content": "1 | visible"}],
        )
        yield event("AssistantMessage", content=[{"text": "Done"}], model="test-model")
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
            tool=lambda *args: lambda handler: handler,
        ),
    )
    call = claude.retrieve(
        repo=tmp_path,
        query_text="issue",
        row=row,
        checkpoint=lambda: None,
        cli="test",
        model="test-model",
        max_turns=5,
    )
    if unexpected_inventory:
        with pytest.raises(ValueError, match="tool isolation differs"):
            asyncio.run(call)
        assert "sdk_result" not in row
        return
    asyncio.run(call)
    assert row["status"] == "ok" and row["sdk_reported_cost_usd"] == 0.03
    assert row["unavailable_tool_attempts"] == [
        {"id": "absent", "name": "mcp__source__graph_neighbors"}
    ]
    assert row["messages"][1]["content"] == rejected
    prediction = claude.native.contextbench_prediction(
        instance_id="case", messages=row["messages"], repo_path=tmp_path
    )
    assert prediction["traj_data"]["pred_steps"][0] == {"files": [], "spans": {}}
    assert prediction["traj_data"]["pred_spans"] == {"a.py": [{"start": 1, "end": 1}]}
    nodes = claude.native.observed_nodes(row["messages"], tmp_path)
    assert len(nodes) == 1 and nodes[0].start_line == nodes[0].end_line == 0


@pytest.mark.parametrize(
    "first_status,expected_calls",
    [
        ("budget_exhausted", 2),
        ("error", 1),
        ("snapshot_error", 0),
        ("post_snapshot_error", 1),
        ("missing_meter", 1),
        ("invalid_meter", 1),
        ("meter_limit", 1),
        ("preparation_error", 1),
        ("graph_observations", 2),
    ],
)
def test_turn_limit_retains_next_case_but_sdk_error_stops_batch(
    monkeypatch, tmp_path, first_status, expected_calls
):
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "manifest.json").write_text("frozen")
    source = prepared / "source.json"
    source.write_text(json.dumps({"status": "ok", "repo": str(tmp_path)}))
    cases = {
        key: SimpleNamespace(
            instance_id=key, problem_statement="issue", base_commit="abc"
        )
        for key in ("first", "second")
    }
    manifest = {"cases": [{"instance_id": key, "source": str(source)} for key in cases]}
    if first_status == "preparation_error":
        failed_source = prepared / "failed-source.json"
        failed_source.write_text(
            json.dumps({"status": "error", "error": "unconfigured gitlink"})
        )
        manifest["cases"][0]["source"] = str(failed_source)
    monkeypatch.setattr(claude.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(
        claude, "subscription_auth", lambda cli: {"subscriptionType": "max"}
    )
    monkeypatch.setattr(claude.native, "load_inputs", lambda path: (manifest, cases))
    monkeypatch.setattr(claude.native, "freeze", lambda path: {})
    monkeypatch.setattr(claude.importlib.metadata, "version", lambda name: "test")
    monkeypatch.setattr(claude.subprocess, "check_output", lambda *a, **kw: "test CLI")
    audits = []

    def audit(*args):
        audits.append(args)
        if first_status == "snapshot_error" or (
            first_status == "post_snapshot_error" and len(audits) > 1
        ):
            raise ValueError("snapshot audit failed")
        return {"clean": True}

    monkeypatch.setattr(claude.native, "audit_contextbench_snapshot", audit)
    monkeypatch.setattr(claude.native, "contextbench_prediction", lambda **kw: {})
    monkeypatch.setattr(claude.native, "add_delivery", lambda *a: None)
    calls = []
    if first_status == "graph_observations":
        monkeypatch.setattr(
            claude,
            "graph_options",
            lambda *args: (
                {key: {"available": True} for key in cases},
                {
                    "enabled": True,
                    "tokens": 2048,
                    "source_policy": "unseen",
                    "policy": claude.GRAPH_QUERY_POLICIES[1],
                },
            ),
        )
        monkeypatch.setattr(
            claude,
            "load_navigation",
            lambda *args, **kwargs: SimpleNamespace(
                tool_spec=lambda: kwargs["observed_source"]
            ),
        )
        (tmp_path / "a.py").write_text("visible\nunseen\n")

    async def retrieve(**kwargs):
        calls.append(kwargs["row"]["instance_id"])
        kwargs["row"]["status"] = (
            first_status
            if kwargs["row"]["instance_id"] == "first"
            and "snapshot" not in first_status
            and "meter" not in first_status
            and first_status != "graph_observations"
            else "ok"
        )
        if first_status == "graph_observations":
            assert kwargs["graph_query_policy"] == claude.GRAPH_QUERY_POLICIES[1]
            assert (
                claude.native.LATE_GRAPH_INSTRUCTIONS
                in kwargs["row"]["messages"][0]["content"]
            )
            observe = kwargs["graph_tool"]
            assert observe() == []  # New task, no shared source state.
            kwargs["row"]["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "read",
                                "function": {
                                    "name": "read",
                                    "arguments": json.dumps(
                                        {"file_path": "a.py", "limit": 200}
                                    ),
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "read", "content": "1 | visible"},
                ]
            )
            nodes = observe()
            assert len(nodes) == 1 and nodes[0].file == "a.py"
            assert nodes[0].start_line == nodes[0].end_line == 0
        if "meter" in first_status:
            kwargs["row"]["sdk_events"] = [{"kind": "ResultMessage"}]
            if first_status == "invalid_meter":
                kwargs["row"]["sdk_reported_cost_usd"] = float("nan")
            elif first_status == "meter_limit":
                kwargs["row"]["sdk_reported_cost_usd"] = 100.0

    monkeypatch.setattr(claude, "retrieve", retrieve)
    output = tmp_path / "result"
    claude.run(
        SimpleNamespace(
            live=True,
            prepared=prepared,
            output=output,
            model="opus",
            max_turns=20,
            graphs=tmp_path if first_status == "graph_observations" else None,
        )
    )
    assert len(calls) == expected_calls
    second = json.loads((output / "second.json").read_text())
    continues = expected_calls == 2 or first_status == "preparation_error"
    assert second["status"] == ("ok" if continues else "not_run")
    assert second["actual_bill_usd"] is None

    usage = json.loads((output / "usage.json").read_text())
    assert usage["cases"] == 2
    if first_status == "preparation_error":
        first = json.loads((output / "first.json").read_text())
        assert first["status"] == "preparation_failed"
        assert first["sdk_reported_cost_usd"] == 0.0
        assert not first["sdk_events"] and "trajectory" not in first
        assert calls == ["second"]
        assert usage["statuses"] == {"preparation_failed": 1, "ok": 1}
    if "snapshot" in first_status:
        first = json.loads((output / "first.json").read_text())
        assert first["status"] == "error"
        assert "snapshot audit failed" in first["error"]
        assert "trajectory" not in first
