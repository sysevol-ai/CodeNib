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


@pytest.mark.parametrize("policy,last_line", [("native", 1), ("functions", 3)])
def test_function_reads_reach_next_model_turn_and_official_trajectory(
    monkeypatch, tmp_path, policy, last_line
):
    (tmp_path / "a.py").write_text(
        "def touched():\n    value = helper()\n    return value\n\n"
        "def helper():\n    return 1\n"
    )
    sent = fake_post(
        monkeypatch,
        [
            tool("read", {"file_path": "a.py", "offset": 1, "limit": 1}, "read"),
            {"role": "assistant", "content": "Found the implementation."},
            {"role": "assistant", "content": "Reviewed."},
        ],
    )
    result = agent.retrieve(
        repo=tmp_path,
        query="Understand touched",
        chat=chat(),
        max_turns=4,
        source_policy=policy,
    )
    delivered = next(m["content"] for m in sent[1]["messages"] if m["role"] == "tool")
    assert "graph_neighbors" not in sent[0]["messages"][0]["content"]
    assert ("value = helper()" in delivered) == (policy == "functions")
    assert "return 1" not in delivered
    predicted = agent.contextbench_prediction(
        instance_id="case", messages=result.messages, repo_path=tmp_path
    )
    assert predicted["traj_data"]["pred_spans"] == {
        "a.py": [{"start": 1, "end": last_line}]
    }


@pytest.mark.parametrize("query_policy", agent.GRAPH_QUERY_POLICIES)
def test_graph_source_reaches_model_and_delivery_without_crediting_declared_range(
    monkeypatch, tmp_path, query_policy
):
    from codenib.agent.tools.spec import ToolSpec

    (tmp_path / "dependency.py").write_text("def target():\n    return 1\nunseen = 2\n")
    observed = json.dumps(
        [
            {
                "file": "dependency.py",
                "definition_start": 1,
                "definition_end": 3,
                "content": "1 | def target():\n2 |     return 1",
            }
        ]
    )
    spec = ToolSpec(
        tool_id="graph_neighbors",
        description="Graph source",
        executor_fn=lambda: observed,
        inputs=[],
    )
    sent = fake_post(
        monkeypatch,
        [
            tool("graph_neighbors", {}, "graph"),
            {"role": "assistant", "content": "Found target."},
            {"role": "assistant", "content": "Reviewed."},
        ],
    )
    result = agent.retrieve(
        repo=tmp_path,
        query="Find dependency",
        chat=chat(),
        max_turns=4,
        graph_tool=spec,
        graph_query_policy=query_policy,
        source_policy="functions",
    )
    assert "graph_neighbors" in {item["function"]["name"] for item in sent[0]["tools"]}
    prompt = sent[0]["messages"][0]["content"]
    assert "graph_neighbors" in prompt
    early = query_policy == agent.GRAPH_QUERY_POLICIES[0]
    assert (agent.GRAPH_INSTRUCTIONS in prompt) == early
    assert (agent.LATE_GRAPH_INSTRUCTIONS in prompt) == (not early)
    assert observed in [
        m.get("content") for m in sent[1]["messages"] if m["role"] == "tool"
    ]
    nodes = agent.observed_nodes(result.messages, tmp_path)
    assert len(nodes) == 1 and nodes[0].end_line == 1
    prediction = agent.contextbench_prediction(
        instance_id="case", messages=result.messages, repo_path=tmp_path
    )
    assert prediction["traj_data"]["pred_spans"] == {
        "dependency.py": [{"start": 1, "end": 2}]
    }


def test_function_completion_grep_keeps_file_identity_and_source_limits(tmp_path):
    (tmp_path / "a.py").write_text("def found():\n    return 3\n")
    (tmp_path / "b.py").write_text("def other():\n    return 4\n")
    bound = agent.bind_context_source_tool(
        lambda **kw: "a.py:1: def found():\nb.py:1: def other():",
        repo=tmp_path,
        tool_id="grep",
        source_policy="functions",
    )
    result = bound(path=".", pattern="def")
    assert "a.py:2:     return 3" in result
    assert "b.py:2:     return 4" in result
    messages = [
        tool("grep", {"pattern": "def"}, "grep"),
        {"role": "tool", "tool_call_id": "grep", "content": result},
    ]
    assert agent.contextbench_prediction(
        instance_id="case", messages=messages, repo_path=tmp_path
    )["traj_data"]["pred_spans"] == {
        "a.py": [{"start": 1, "end": 2}],
        "b.py": [{"start": 1, "end": 2}],
    }
    with pytest.raises(ValueError, match="escapes"):
        bound(path="..", pattern="def")


@pytest.mark.parametrize("policy", ["definitions", "unseen"])
def test_completion_uses_prior_model_inputs_without_hiding_original_reads(
    monkeypatch, tmp_path, policy
):
    (tmp_path / "a.py").write_text("def found():\n    value = 3\n    return value\n")
    sent = fake_post(
        monkeypatch,
        [
            tool("read", {"file_path": "a.py", "offset": 1, "limit": 1}, "first"),
            tool("read", {"file_path": "a.py", "offset": 2, "limit": 1}, "second"),
            {"role": "assistant", "content": "Found it."},
            {"role": "assistant", "content": "Reviewed."},
        ],
    )
    result = agent.retrieve(
        repo=tmp_path,
        query="Inspect found",
        chat=chat(),
        max_turns=5,
        source_policy="functions",
        completion_source_policy=policy,
    )
    second = next(
        message["content"]
        for message in sent[2]["messages"]
        if message.get("tool_call_id") == "second"
    )
    assert "value = 3" in second
    assert ("return value" in second) == (policy == "definitions")
    assert ("def found" in second) == (policy == "definitions")
    assert agent.contextbench_prediction(
        instance_id="case", messages=result.messages, repo_path=tmp_path
    )["traj_data"]["pred_spans"] == {"a.py": [{"start": 1, "end": 3}]}


def test_unseen_completion_only_uses_actual_prior_source_and_current_targets(tmp_path):
    (tmp_path / "a.py").write_text("def found():\n    value = 3\n    return value\n")
    (tmp_path / "b.py").write_text("def unrelated():\n    return 'not requested'\n")
    history = [
        tool("read", {"file_path": "a.py", "limit": 100}, "prior"),
        {"role": "tool", "tool_call_id": "prior", "content": "2 |     value = 3"},
        tool("read", {"file_path": "b.py", "limit": 100}, "other"),
        {"role": "tool", "tool_call_id": "other", "content": "1 | def unrelated():"},
    ]
    original = "a.py:1: def found():"
    bound = agent.bind_context_source_tool(
        lambda **kw: original,
        repo=tmp_path,
        tool_id="grep",
        source_policy="functions",
        completion_source_policy="unseen",
        observed_source=lambda: agent.observed_nodes(history, tmp_path),
    )
    result = bound(path="a.py", pattern="found")
    assert result.startswith(original)
    assert "return value" in result
    assert "value = 3" not in result and "not requested" not in result
    # Stale or truncated text cannot count as observed, even with a large request.
    history[1]["content"] = "2 |     value = 3 ... [truncated]"
    assert "value = 3" in bound(path="a.py", pattern="found")
    with pytest.raises(ValueError, match="observed source receipts"):
        agent.bind_context_source_tool(
            lambda **kw: original,
            repo=tmp_path,
            tool_id="grep",
            source_policy="functions",
            completion_source_policy="unseen",
        )


def test_unseen_completion_does_not_refill_budget_with_unselected_functions(tmp_path):
    (tmp_path / "a.py").write_text(
        "def first():\n    value = 3\n    return value\n\n"
        "def second():\n    return 4\n"
    )
    history = [
        tool("read", {"file_path": "a.py"}, "prior"),
        {
            "role": "tool",
            "tool_call_id": "prior",
            "content": "1 | def first():\n2 |     value = 3\n3 |     return value",
        },
    ]
    first = "a.py:1: def first():"
    original = first + "\na.py:5: def second():"
    probe = agent.bind_context_source_tool(
        lambda **kw: first, repo=tmp_path, tool_id="grep", source_policy="functions"
    )(path="a.py", pattern="first")
    budget = len(tiktoken.get_encoding("cl100k_base").encode(probe[len(first) :]))
    for policy in ("definitions", "unseen"):
        bound = agent.bind_context_source_tool(
            lambda **kw: original,
            repo=tmp_path,
            tool_id="grep",
            source_policy="functions",
            completion_source_policy=policy,
            completion_tokens=budget,
            observed_source=lambda: agent.observed_nodes(history, tmp_path),
        )
        result = bound(path="a.py", pattern="first|second")
        assert result.startswith(original)
        assert ("return value" in result) == (policy == "definitions")
        assert "return 4" not in result and "not completed" in result


@pytest.mark.parametrize("budget,huge", [(1, False), (8192, True)])
def test_function_completion_never_partially_delivers_an_added_callable(
    tmp_path, budget, huge
):
    source = (
        "def found():\n    value = '"
        + ("x" * 17000 if huge else "small")
        + "'\n    return value\n"
    )
    (tmp_path / "a.py").write_text(source)
    bound = agent.bind_context_source_tool(
        lambda **kw: "1 | def found():",
        repo=tmp_path,
        tool_id="read",
        source_policy="functions",
        completion_tokens=budget,
    )
    result = bound(file_path="a.py")
    assert "value =" not in result
    assert "not completed" in result
    assert len(result) <= agent._MAX_RESULT_CHARS


def test_completion_does_not_expand_names_only_or_truncated_source(tmp_path):
    (tmp_path / "a.py").write_text("def found():\n    return 3\n")
    for tool_id, text, args in (
        ("grep", "a.py", {"output_mode": "files_with_matches"}),
        ("read", "1 | def fou ... [line truncated]", {"file_path": "a.py"}),
        ("glob", "a.py", {}),
    ):
        bound = agent.bind_context_source_tool(
            lambda text=text, **kw: text,
            repo=tmp_path,
            tool_id=tool_id,
            source_policy="functions",
        )
        assert bound(**args) == text


@pytest.fixture
def paired_runs(monkeypatch, tmp_path):
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "manifest.json").write_text("frozen")
    cases = {key: SimpleNamespace(instance_id=key) for key in ("ok", "failed")}
    monkeypatch.setattr(agent, "load_inputs", lambda path: ({}, cases))
    paths = []
    for policy, recall, precision in (("native", 1, 0.2), ("functions", 0.5, 0.5)):
        directory = tmp_path / policy
        (directory / "evaluation" / "trajectory").mkdir(parents=True)
        config = {
            "prepared": str(prepared),
            "manifest_sha256": agent.digest(prepared / "manifest.json"),
            "implementation": {},
            "source_policy": policy,
            "parameters": {"model": "same-model"},
            "max_turns": 20,
        }
        (directory / "config.json").write_text(json.dumps(config))
        scores = []
        for key in cases:
            record = {
                "instance_id": key,
                "status": "ok" if key == "ok" else "error",
                "calls": [{"model": "actual-model"}],
            }
            (directory / f"{key}.json").write_text(json.dumps(record))
            scores.append(
                {
                    "instance_id": key,
                    **(
                        {
                            "final": {
                                "line": {"coverage": recall, "precision": precision}
                            }
                        }
                        if key == "ok"
                        else {"error": "missing_prediction"}
                    ),
                }
            )
        (directory / "evaluation" / "trajectory" / "metrics.json").write_text(
            json.dumps({"cases": scores})
        )
        (directory / "usage.json").write_text(json.dumps({"new_api_cost_usd": 1}))
        paths.append(directory)
    return SimpleNamespace(
        baseline=paths[0], candidate=paths[1], output=tmp_path / "comparison.json"
    )


def test_paired_online_comparison_keeps_failures_and_macro_f1(paired_runs):
    agent.compare(paired_runs)
    result = json.loads(paired_runs.output.read_text())
    assert result["n"] == 2
    assert result["metrics"]["recall"]["baseline"] == 0.5
    assert result["metrics"]["f1"]["baseline"] == pytest.approx(1 / 6)
    assert result["metrics"]["f1"]["candidate"] == 0.25
    assert all(arm["scoring_failures"] == 1 for arm in result["arms"])
    assert "pass_at_1" not in result


def test_completion_source_comparison_rejects_confounded_graph_change(paired_runs):
    for directory, policy in (
        (paired_runs.baseline, "definitions"),
        (paired_runs.candidate, "unseen"),
    ):
        path = directory / "config.json"
        config = json.loads(path.read_text())
        config.update(source_policy="functions", completion_source_policy=policy)
        path.write_text(json.dumps(config))
    agent.compare(paired_runs)
    assert (
        json.loads(paired_runs.output.read_text())["contrast"]
        == "unseen_function_source_over_definitions"
    )
    path = paired_runs.candidate / "config.json"
    config["graph"] = {"enabled": True}
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="completion_source_policy"):
        agent.compare(paired_runs)


def test_graph_timing_comparison_rejects_source_budget_and_tool_confounders(
    paired_runs,
):
    for directory, policy in zip(
        (paired_runs.baseline, paired_runs.candidate),
        agent.GRAPH_QUERY_POLICIES,
        strict=True,
    ):
        path = directory / "config.json"
        config = json.loads(path.read_text())
        config.update(source_policy="functions", tools=["read", "graph_neighbors"])
        config["graph"] = {
            "enabled": True,
            "tokens": 2048,
            "source_policy": "unseen",
            "policy": policy,
        }
        path.write_text(json.dumps(config))
    agent.compare(paired_runs)
    assert (
        json.loads(paired_runs.output.read_text())["contrast"]
        == "late_graph_query_over_first_source"
    )
    path = paired_runs.candidate / "config.json"
    for key, value in (("tokens", 4096), ("source_policy", "definitions")):
        changed = copy.deepcopy(config)
        changed["graph"][key] = value
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="graph-source controls"):
            agent.compare(paired_runs)
    config["tools"].append("bash")
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="differ in tools"):
        agent.compare(paired_runs)


def test_graph_source_comparison_keeps_inputs_budgets_and_tools_matched(paired_runs):
    for directory, source_policy in (
        (paired_runs.baseline, "definitions"),
        (paired_runs.candidate, "unseen"),
    ):
        path = directory / "config.json"
        config = json.loads(path.read_text())
        config.update(
            source_policy="functions", tools=["read", "grep", "glob", "graph_neighbors"]
        )
        config["graph"] = {
            "enabled": True,
            "tokens": 2048,
            "source_policy": source_policy,
        }
        path.write_text(json.dumps(config))
    agent.compare(paired_runs)
    assert (
        json.loads(paired_runs.output.read_text())["contrast"]
        == "unseen_graph_source_over_definitions"
    )
    path = paired_runs.candidate / "config.json"
    config = json.loads(path.read_text())
    config["graph"]["tokens"] = 4096
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="graph-source controls"):
        agent.compare(paired_runs)
    config["graph"]["tokens"] = 2048
    config["tools"].append("bash")
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="differ in tools"):
        agent.compare(paired_runs)


@pytest.mark.parametrize("baseline_policy", ["functions", "native"])
@pytest.mark.parametrize("empty_fallback", [False, True])
def test_graph_comparison_permits_only_the_declared_tool_difference(
    paired_runs, monkeypatch, baseline_policy, empty_fallback
):
    graph_dir = paired_runs.output.parent / "graphs"
    graph_dir.mkdir()
    (graph_dir / "manifest.json").write_text("frozen graphs")
    monkeypatch.setattr(agent, "load_graph_records", lambda *a: {})
    for directory, enabled in (
        (paired_runs.baseline, False),
        (paired_runs.candidate, True),
    ):
        path = directory / "config.json"
        config = json.loads(path.read_text())
        config.update(
            source_policy="functions" if enabled else baseline_policy,
            backend="claude-agent-sdk",
            tools=["mcp__source__read", "mcp__source__grep", "mcp__source__glob"],
        )
        config["graph"] = {
            "enabled": enabled,
            "prepared": str(graph_dir),
            "manifest_sha256": agent.digest(graph_dir / "manifest.json"),
            "tokens": 2048,
            "policy": "late_issue_reference_hop",
            "source_policy": "unseen",
            "empty_fallback": enabled and empty_fallback,
        }
        if enabled:
            config["tools"].append("mcp__source__graph_neighbors")
        path.write_text(json.dumps(config))
    agent.compare(paired_runs)
    expected = (
        "directed_graph_over_function_completion"
        if baseline_policy == "functions"
        else "functions_and_directed_graph_over_native"
    )
    if empty_fallback:
        expected = (
            "late_empty_graph_over_function_completion"
            if baseline_policy == "functions"
            else "functions_and_late_empty_graph_over_native"
        )
    assert json.loads(paired_runs.output.read_text())["contrast"] == expected
    path = paired_runs.candidate / "config.json"
    config = json.loads(path.read_text())
    for key, value in (("tokens", 4096), ("policy", agent.GRAPH_QUERY_POLICIES[0])):
        changed = copy.deepcopy(config)
        changed["graph"][key] = value
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="graph-source controls"):
            agent.compare(paired_runs)
    config["tools"].append("Bash")
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="Only graph_neighbors"):
        agent.compare(paired_runs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("backend", "claude-agent-sdk"),
        ("max_turns", 40),
        ("implementation", {"changed.py": "other"}),
    ],
)
def test_paired_comparison_rejects_changed_harness_or_budget(paired_runs, field, value):
    path = paired_runs.candidate / "config.json"
    config = json.loads(path.read_text())
    config[field] = value
    if field == "implementation":
        archive = paired_runs.candidate / "implementation"
        archive.mkdir()
        (archive / "changed.py").write_text("different")
        config[field]["changed.py"] = agent.digest(archive / "changed.py")
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=f"differ in {field}"):
        agent.compare(paired_runs)
    assert not paired_runs.output.exists()


def test_paired_comparison_rejects_missing_score_rows(paired_runs):
    path = paired_runs.candidate / "evaluation" / "trajectory" / "metrics.json"
    scores = json.loads(path.read_text())
    scores["cases"].pop()
    path.write_text(json.dumps(scores))
    with pytest.raises(ValueError, match="identities"):
        agent.compare(paired_runs)


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
