"""Packet source credit requires a real confirmed input, never prepared spans."""

import asyncio
import copy
import hashlib
import json
import sys
from dataclasses import make_dataclass
from types import SimpleNamespace

import pytest

from scripts import contextbench_packet_reader as reader


def event(kind, **fields):
    return make_dataclass(kind, [(name, object) for name in fields])(**fields)


def install_sdk(monkeypatch, query):
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            ClaudeAgentOptions=SimpleNamespace,
            query=query,
        ),
    )
    monkeypatch.setattr(reader.native, "audit_contextbench_snapshot", lambda *a: {})


def input_packet(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("visible\nunseen\n")
    return json.dumps([{"file": "a.py", "content": "2 | unseen\n"}]), {
        "a.py": hashlib.sha256(path.read_bytes()).hexdigest()
    }


def inspect(tmp_path, context, hashes, row, checkpoint=lambda: None):
    return asyncio.run(
        reader.inspect_packet(
            repo=tmp_path,
            commit="pinned",
            context=context,
            source_hashes=hashes,
            issue="Find the missing behavior",
            row=row,
            checkpoint=checkpoint,
            cli="test",
            model="test-model",
        )
    )


def init():
    return event(
        "SystemMessage",
        subtype="init",
        data={
            "apiKeySource": "ANTHROPIC_API_KEY",
            "tools": [],
        },
    )


def result():
    return event(
        "ResultMessage",
        subtype="success",
        is_error=False,
        total_cost_usd=0.02,
        num_turns=1,
        result="done",
    )


def test_exact_packet_is_confirmed_before_source_credit(monkeypatch, tmp_path):
    context, hashes = input_packet(tmp_path)
    row = {"instance_id": "case"}
    checkpoints = []

    async def query(*, prompt, options):
        assert prompt == row["messages"][0]["content"] and prompt.endswith(context)
        assert reader.text_hash(prompt) == row["input_receipt"]["prompt_sha256"]
        assert not row["input_receipt"]["confirmed"]
        assert "packet_prediction" not in row
        assert options.tools == options.allowed_tools == []
        assert options.mcp_servers == {} and options.strict_mcp_config
        assert options.setting_sources == options.skills == options.plugins == []
        assert options.max_turns == 1 and options.max_budget_usd == 0.25
        yield init()
        yield event(
            "AssistantMessage", model="test-model", content=[{"text": "a.py:2"}]
        )
        assert row["input_receipt"]["confirmed"]
        assert "packet_prediction" not in row  # terminal/source audit still pending
        yield result()

    install_sdk(monkeypatch, query)
    inspect(
        tmp_path, context, hashes, row, lambda: checkpoints.append(copy.deepcopy(row))
    )
    assert row["status"] == "ok" and row["sdk_reported_cost_usd"] == 0.02
    assert row["packet_prediction"]["traj_data"]["pred_spans"] == {
        "a.py": [{"start": 2, "end": 2}]
    }
    assert checkpoints[0]["input_receipt"]["confirmed"] is False


def test_no_assistant_response_means_no_source_credit(monkeypatch, tmp_path):
    context, hashes = input_packet(tmp_path)
    row = {
        "instance_id": "case",
        "packet_prediction": {"stale": "must not survive an unconfirmed input"},
        "input_receipt": {"confirmed": True},
        "sdk_result": {"subtype": "success"},
    }

    async def query(**kwargs):
        yield init()
        yield result()

    install_sdk(monkeypatch, query)
    with pytest.raises(ValueError):
        inspect(tmp_path, context, hashes, row)
    assert not row["input_receipt"]["confirmed"]
    assert "packet_prediction" not in row and row["sdk_reported_cost_usd"] == 0.02


def test_terminal_usage_survives_post_result_sdk_error(monkeypatch, tmp_path):
    context, hashes = input_packet(tmp_path)
    row = {"instance_id": "case"}

    async def query(**kwargs):
        yield init()
        yield event("AssistantMessage", model="test-model", content=[{"text": "done"}])
        yield result()
        raise RuntimeError("SDK iteration failed after usage")

    install_sdk(monkeypatch, query)
    with pytest.raises(RuntimeError, match="after usage"):
        inspect(tmp_path, context, hashes, row)
    assert row["sdk_reported_cost_usd"] == 0.02
    assert "packet_prediction" not in row


@pytest.mark.parametrize("violation", ["model", "tool", "route", "compaction"])
def test_isolation_failures_retain_usage_without_credit(
    monkeypatch, tmp_path, violation
):
    context, hashes = input_packet(tmp_path)
    row = {"instance_id": "case"}

    async def query(**kwargs):
        yield event(
            "SystemMessage",
            subtype="init",
            data={
                "apiKeySource": "none" if violation == "route" else "ANTHROPIC_API_KEY",
                "tools": [],
            },
        )
        if violation == "compaction":
            yield event("SystemMessage", subtype="compact_boundary", data={})
        blocks = [{"text": "done"}]
        if violation == "tool":
            blocks.append({"name": "Bash", "id": "tool", "input": {}})
        yield event(
            "AssistantMessage",
            model="wrong" if violation == "model" else "test-model",
            content=blocks,
        )
        yield result()

    install_sdk(monkeypatch, query)
    with pytest.raises(ValueError):
        inspect(tmp_path, context, hashes, row)
    assert row["sdk_reported_cost_usd"] == 0.02
    assert "packet_prediction" not in row


def test_source_change_before_completion_cannot_publish_credit(monkeypatch, tmp_path):
    context, hashes = input_packet(tmp_path)
    row = {"instance_id": "case"}

    async def query(**kwargs):
        yield init()
        yield event("AssistantMessage", model="test-model", content=[{"text": "done"}])
        (tmp_path / "a.py").write_text("changed\nunseen\n")
        yield result()

    install_sdk(monkeypatch, query)
    with pytest.raises(ValueError, match="version changed"):
        inspect(tmp_path, context, hashes, row)
    assert "packet_prediction" not in row and row["sdk_reported_cost_usd"] == 0.02


def test_fabricated_or_partial_source_is_rejected_before_dispatch(
    monkeypatch, tmp_path
):
    _, hashes = input_packet(tmp_path)
    row = {"instance_id": "case"}

    async def query(**kwargs):
        pytest.fail("Invalid packet must never reach the SDK")
        yield

    install_sdk(monkeypatch, query)
    for text in ("2 | uns", "2 | unseen\n3 | invented\n", "unseen"):
        with pytest.raises(ValueError):
            inspect(
                tmp_path, json.dumps([{"file": "a.py", "content": text}]), hashes, row
            )
        assert not row["api_attempted"] and "packet_prediction" not in row
