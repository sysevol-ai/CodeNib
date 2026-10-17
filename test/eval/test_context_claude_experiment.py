"""Subscription benchmark boundaries; no SDK installation or live calls required."""

import json
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
    with pytest.raises(ValueError, match="Unexpected SDK tool"):
        claude.normalize_event(
            "AssistantMessage",
            {"content": [{"id": "unexpected", "name": "Bash", "input": {}}]},
        )


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
    with pytest.raises(ValueError, match="Unexpected SDK tool"):
        claude.normalize_event("AssistantMessage", event)
    normalized = claude.normalize_event(
        "AssistantMessage",
        event,
        {**claude.TOOLS, "mcp__source__graph_neighbors": "graph_neighbors"},
    )
    assert normalized[0]["tool_calls"][0]["function"]["name"] == "graph_neighbors"


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

    async def retrieve(**kwargs):
        calls.append(kwargs["row"]["instance_id"])
        kwargs["row"]["status"] = (
            first_status
            if kwargs["row"]["instance_id"] == "first"
            and "snapshot" not in first_status
            and "meter" not in first_status
            else "ok"
        )
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
            live=True, prepared=prepared, output=output, model="opus", max_turns=20
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
