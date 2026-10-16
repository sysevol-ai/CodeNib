"""Native retrieval-agent regression tests: real local tools, fake model calls."""

import copy
import json
from types import SimpleNamespace

import pytest
import tiktoken

from codenib.agent.skills.core import SkillMetadata, SkillType
from scripts import benchmark_context_agent as agent
from scripts.benchmark_decision_loop import BudgetReached, Spend


def response(message, cost=0.01):
    return {
        "id": "fake-response",
        "model": "anthropic/claude-opus-5.5",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cost": cost,
        },
    }


def tool(name, arguments, identifier):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def fake_post(monkeypatch, messages):
    sent = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")

    def post(url, **kwargs):
        sent.append(copy.deepcopy(kwargs["json"]))
        body = response(messages[len(sent) - 1])

        class Result:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return body

        return Result()

    monkeypatch.setattr(agent.requests, "post", post)
    return sent


def chat():
    return agent.OpenRouterToolChat(
        parameters={"model": "anthropic/claude-opus-5.5", "max_tokens": 4096},
        spend=Spend(10, 1),
        checkpoint=lambda: None,
    )


def test_native_multihop_preserves_context_beyond_4k(monkeypatch, tmp_path):
    registry = agent.SkillRegistry()
    monkeypatch.setattr(
        registry,
        "_skills",
        {
            "unrelated": SkillMetadata(
                skill_id="unrelated", skill_type=SkillType.RETRIEVAL
            )
        },
    )
    (tmp_path / "main.py").write_text(
        "\n".join(f"value_{i} = 'helper.py'" for i in range(1, 301)) + "\n"
    )
    (tmp_path / "helper.py").write_text(
        "\n".join(f"dependency_{i} = {i}" for i in range(1, 301)) + "\n"
    )
    first = tool("read", {"file_path": "main.py", "offset": 1, "limit": 300}, "main")
    first["reasoning_details"] = [
        {"type": "reasoning.text", "text": "Inspect dependency."}
    ]
    sent = fake_post(
        monkeypatch,
        [
            first,
            tool(
                "read", {"file_path": "helper.py", "offset": 1, "limit": 300}, "helper"
            ),
            {
                "role": "assistant",
                "content": "main.py and helper.py contain the relevant behavior.",
            },
            {"role": "assistant", "content": "Verified both implementations."},
        ],
    )
    client = chat()
    result = agent.retrieve(
        repo=tmp_path, query="Inspect the dependency behavior", chat=client, max_turns=6
    )
    assert {t["function"]["name"] for t in sent[0]["tools"]} == {"read", "grep", "glob"}
    assert sent[0]["tool_choice"] == "auto"
    assert "helper.py" in sent[1]["messages"][-1]["content"]
    assert any(m.get("reasoning_details") for m in sent[1]["messages"])
    assert len([m for m in sent[-1]["messages"] if m["role"] == "tool"]) == 2
    converted = agent.contextbench_prediction(
        instance_id="case", messages=result.messages, repo_path=tmp_path
    )
    assert converted["traj_data"]["pred_spans"] == {
        "main.py": [{"start": 1, "end": 300}],
        "helper.py": [{"start": 1, "end": 300}],
    }
    nodes = agent.observed_nodes(result.messages, tmp_path)
    context, tokens = agent.pack_context(
        nodes,
        top_k=len(nodes),
        token_budget=32768,
        encoder=tiktoken.get_encoding("cl100k_base"),
    )
    assert tokens > 4096
    assert sum(len(n["content"].splitlines()) for n in json.loads(context)) == 600
    assert client.spend.total == pytest.approx(0.04)
    assert len(client.calls) == 4
    replay = agent.OpenRouterToolChat(
        parameters=client.parameters,
        spend=Spend(1, 1),
        checkpoint=lambda: None,
        replay=client.calls,
    )

    def forbid_execution(*args, **kwargs):
        raise AssertionError("Cached tool observations must not execute again")

    monkeypatch.setattr(agent, "_bind_source_tool", lambda *a, **k: forbid_execution)
    resumed = agent.retrieve(
        repo=tmp_path, query="Inspect the dependency behavior", chat=replay, max_turns=6
    )
    assert resumed.messages == result.messages
    assert replay.spend.total == 0 and len(sent) == 4


def test_successful_prefix_replay_does_not_rebill_and_checks_history(monkeypatch):
    sent = fake_post(monkeypatch, [{"role": "assistant", "content": "done"}])
    original = chat()
    messages = [{"role": "user", "content": "issue"}]
    original._call_raw(messages)
    replay = chat()
    replay.replay = copy.deepcopy(original.calls)
    assert replay._call_raw(messages).choices[0].message.content == "done"
    assert len(sent) == 1 and replay.spend.total == 0
    assert replay.calls[0]["replayed"]
    replay.replay = copy.deepcopy(original.calls)
    with pytest.raises(ValueError, match="differs"):
        replay._call_raw([{"role": "user", "content": "changed issue"}])
    assert len(sent) == 1


def test_serving_union_keeps_gaps_and_excludes_unseen_source(tmp_path):
    (tmp_path / "a.py").write_text("one\nunseen\nthree\npartial suffix\n")
    messages = [
        tool("read", {"file_path": "a.py"}, "read"),
        {
            "role": "tool",
            "tool_call_id": "read",
            "content": "1 | one\n3 | three\n4 | partial\n",
        },
    ]
    nodes = agent.observed_nodes(messages, tmp_path)
    assert [(n.start_line, n.end_line, n.content) for n in nodes] == [
        (0, 0, "one"),
        (2, 2, "three"),
    ]


def test_grep_format_separator_does_not_discard_seen_source(tmp_path):
    (tmp_path / "a.py").write_text("value = 1\n    indented = 2\n")
    messages = [
        tool("grep", {"pattern": "value|indented"}, "grep"),
        {
            "role": "tool",
            "tool_call_id": "grep",
            "content": "a.py:1: value = 1\na.py:2:     indented = 2\n",
        },
    ]
    nodes = agent.observed_nodes(messages, tmp_path)
    assert len(nodes) == 1
    assert nodes[0].content == "value = 1\n    indented = 2"


def test_budget_stop_retains_last_delivered_history_without_new_call(monkeypatch):
    sent = fake_post(monkeypatch, [{"role": "assistant", "content": "done"}])
    client = chat()
    client.spend = Spend(0.005, 0.005)
    messages = [{"role": "user", "content": "issue"}]
    client._call_raw(messages)
    with pytest.raises(BudgetReached):
        client._call_raw(messages)
    assert len(sent) == 1
    assert client.delivered_messages[-1]["content"] == "done"


def test_unknown_charge_stops_subsequent_calls(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")

    def fail(*args, **kwargs):
        raise TimeoutError("unknown billing")

    monkeypatch.setattr(agent.requests, "post", fail)
    client = chat()
    with pytest.raises(TimeoutError):
        client._call_raw([{"role": "user", "content": "issue"}])
    assert client.spend.uncertain
    with pytest.raises(RuntimeError, match="unpriced"):
        client._call_raw([{"role": "user", "content": "issue"}])
    assert len(client.calls) == 1
    other = chat()
    other.cancel = client.cancel
    with pytest.raises(RuntimeError, match="Another worker"):
        other._call_raw([{"role": "user", "content": "different issue"}])
    assert other.calls == [] and other.spend.total == 0


def test_native_tools_reject_path_escape(monkeypatch, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("not in repository")
    repo = tmp_path / "repo"
    repo.mkdir()
    sent = fake_post(
        monkeypatch,
        [
            tool("read", {"file_path": "../outside.txt"}, "escape"),
            {"role": "assistant", "content": "No permitted source found."},
        ],
    )
    agent.retrieve(repo=repo, query="read the file", chat=chat(), max_turns=2)
    content = sent[1]["messages"][-1]["content"]
    assert "escapes benchmark checkout" in content
    assert "not in repository" not in content


def test_function_completion_reads_only_touched_callables(tmp_path):
    source = (
        "class Example:\n"
        "    def touched(self):\n"
        "        value = 1\n"
        "        return value\n"
        "\n"
        "    def unrelated(self):\n"
        "        return 'do not read'\n"
    )
    (tmp_path / "a.py").write_text(source)
    seed = agent.NodeInfo(
        node_id="seed",
        file="a.py",
        start_line=2,
        end_line=2,
        content="        value = 1",
    )
    additions, skipped = agent.complete_observed_functions([seed], tmp_path)
    assert not skipped and len(additions) == 1
    assert (additions[0].start_line, additions[0].end_line) == (1, 3)
    assert additions[0].content == "\n".join(source.splitlines()[1:4])
    assert "unrelated" not in additions[0].content
    assert agent.complete_observed_functions(additions, tmp_path) == ([], [])
    additions, skipped = agent.complete_observed_functions(
        [seed], tmp_path, max_function_lines=2
    )
    assert not additions and skipped == [{"file": "a.py", "start": 2, "end": 4}]


def test_decision_candidates_cover_every_seen_line_without_filling_gaps():
    nodes = [
        agent.NodeInfo(
            node_id="first",
            file="a.py",
            start_line=3,
            end_line=6,
            content="first\n" + "x" * 3100 + "\nthird\n",
        ),
        agent.NodeInfo(
            node_id="last", file="a.py", start_line=9, end_line=9, content="last"
        ),
    ]
    split = agent.decision_candidates(nodes)
    actual = {
        (n.file, n.start_line + i): text
        for n in split
        for i, text in enumerate(n.content.split("\n"))
    }
    assert actual == {
        ("a.py", 3): "first",
        ("a.py", 4): "x" * 3100,
        ("a.py", 5): "third",
        ("a.py", 6): "",
        ("a.py", 9): "last",
    }
    assert all(n.end_line - n.start_line < 100 for n in split)


def test_native_source_preparation_keeps_failures_and_rejects_drift(
    monkeypatch, tmp_path
):
    cases = [SimpleNamespace(instance_id=i, base_commit="abc") for i in ("a", "b")]
    selected = tmp_path / "input.jsonl"
    selected.write_text("frozen input\n")
    monkeypatch.setattr(agent, "load_contextbench_cases", lambda path: cases)
    monkeypatch.setattr(agent, "freeze", lambda output: {})
    monkeypatch.setattr(
        agent, "audit_contextbench_snapshot", lambda *a: {"clean": True}
    )

    def checkout(case, root):
        if case.instance_id == "b":
            raise RuntimeError("checkout unavailable")
        return tmp_path

    monkeypatch.setattr(agent, "prepare_contextbench_repo", checkout)
    output = tmp_path / "prepared"
    agent.prepare(SimpleNamespace(cases=selected, output=output))
    manifest, loaded = agent.load_inputs(output)
    assert set(loaded) == {"a", "b"}
    assert "baseline" not in manifest
    assert json.loads((output / "cases/b.json").read_text())["status"] == "error"
    (output / "cases/a.json").write_text("changed")
    with pytest.raises(ValueError, match="source record changed"):
        agent.load_inputs(output)


@pytest.mark.parametrize("dirty", [False, True])
def test_completed_repack_audits_snapshot_before_reading(monkeypatch, tmp_path, dirty):
    prepared, previous = tmp_path / "prepared", tmp_path / "previous"
    prepared.mkdir()
    previous.mkdir()
    (prepared / "manifest.json").write_text("frozen")
    source = prepared / "source.json"
    source.write_text(json.dumps({"repo": str(tmp_path / "repo")}))
    case = SimpleNamespace(instance_id="case", base_commit="a" * 40)
    manifest = {"cases": [{"instance_id": "case", "source": str(source)}]}
    parameters = {
        "model": "anthropic/claude-opus-5.5",
        "max_tokens": 4096,
        "reasoning": {"effort": "medium"},
    }
    (previous / "config.json").write_text(
        json.dumps(
            {
                "manifest_sha256": agent.digest(prepared / "manifest.json"),
                "parameters": parameters,
                "max_turns": 20,
            }
        )
    )
    (previous / "case.json").write_text(
        json.dumps(
            {
                "instance_id": "case",
                "status": "ok",
                "calls": [],
                "served_16384": {},
                "served_32768": {},
            }
        )
    )
    monkeypatch.setattr(agent, "load_inputs", lambda _path: (manifest, {"case": case}))
    monkeypatch.setattr(agent, "freeze", lambda _path: {})
    events = []

    def audit(*_args):
        events.append("audit")
        if dirty:
            raise ValueError("snapshot audit failed")
        return {"clean": True}

    monkeypatch.setattr(agent, "audit_contextbench_snapshot", audit)
    monkeypatch.setattr(agent, "add_delivery", lambda *_args: events.append("read"))
    output = tmp_path / "output"
    agent.run(
        SimpleNamespace(
            command="repack",
            max_turns=20,
            workers=1,
            max_cost_usd=1,
            case_cost_usd=1,
            prepared=prepared,
            output=output,
            resume_from=previous,
        )
    )
    row = json.loads((output / "case.json").read_text())
    assert row["status"] == ("error" if dirty else "ok")
    assert events == (["audit"] if dirty else ["audit", "read", "audit"])
    if dirty:
        assert "served_16384" not in row and "served_32768" not in row
    assert (output / "usage.json").is_file()
