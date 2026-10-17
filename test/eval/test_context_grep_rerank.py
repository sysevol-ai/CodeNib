"""Real grep/source formatting with deterministic decision and SDK responses."""

import asyncio
import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import make_dataclass
from types import SimpleNamespace

import pytest

from codenib.agent.tools.defaults import get_default_tool_specs
from codenib.llm.decisions import DecisionResult, ScoreAnswer
from scripts import benchmark_context_agent as native
from scripts import benchmark_context_claude as claude
from scripts import contextbench_grep_rerank as grep


@pytest.fixture
def source(tmp_path):
    content = (
        "def noisy():\n    marker = 1\n"
        + "".join(
            f"    junk_{i} = 'irrelevant repeated padding value'\n" for i in range(30)
        )
        + "\ndef target():\n    marker = 1\n    return helper()\n"
    )
    (tmp_path / "a.py").write_text(content)
    spec = next(t for t in get_default_tool_specs() if t.tool_id == "grep")
    execute = native.bind_context_source_tool(
        spec.executor_fn, repo=tmp_path, tool_id="grep"
    )
    return tmp_path, execute


def decisions(self, *, state, questions):
    answers = {}
    for name, question in questions.items():
        score = 3 if "def target" in state["candidates"][name]["content"] else 0
        answers[name] = ScoreAnswer(
            type="score",
            score=float(score),
            confidence=1.0,
            probabilities={str(i): float(i == score) for i in range(4)},
            legend={str(i): value for i, value in enumerate(question.criteria)},
        )
    return DecisionResult(
        model=grep.JEV_MODEL + "-20260917", answers=answers, usage={"cost": 0.0001}
    )


def test_same_grep_pool_is_ranked_before_exact_bounded_delivery(monkeypatch, source):
    repo, execute = source
    monkeypatch.setattr(grep.OpenRouterDecisions, "decide", decisions)
    views = [
        grep.GrepSourceView(repo=repo, query="Find target", policy=p, token_budget=256)
        for p in ("chunks", "jev")
    ]
    replies = [v.wrap(execute)(pattern="marker", path=".") for v in views]
    traces = [v.ledger["views"][0] for v in views]
    assert traces[0]["candidates"] == traces[1]["candidates"]
    assert traces[0]["order"] != traces[1]["order"]
    assert "def target" in replies[1].splitlines()[0]
    assert views[0].ledger["calls"] == []
    assert views[1].ledger["cost_usd"] == 0.0001
    for view, reply in zip(views, replies, strict=True):
        assert len(view.encoder.encode(reply)) <= 256
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "g", "function": {"name": "grep", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "g", "content": reply},
        ]
        observed = native.observed_nodes(messages, repo)
        returned = sum(len(n.content.splitlines()) for n in observed)
        assert returned == sum(line.startswith("a.py:") for line in reply.splitlines())
        assert returned < len((repo / "a.py").read_text().splitlines())
        prediction = native.contextbench_prediction(
            instance_id="case", repo_path=repo, messages=messages
        )
        assert prediction["traj_data"]["pred_spans"]


def test_search_hints_preserve_hits_and_add_no_unread_source_credit(
    monkeypatch, source
):
    repo, execute = source
    queries = []

    def decide(self, **kwargs):
        queries.append(kwargs["state"]["query"])
        return decisions(self, **kwargs)

    monkeypatch.setattr(grep.OpenRouterDecisions, "decide", decide)
    args = {"pattern": "marker", "path": "."}
    original = execute(**args)
    views = [
        grep.GrepSourceView(repo=repo, query="Issue context", policy=policy)
        for policy in ("hints", "jev-hints")
    ]
    replies = [view.wrap(execute)(**args) for view in views]
    assert len(queries) == 1
    assert "Issue context" in queries[0] and json.dumps(args) in queries[0]
    assert views[0].description == views[1].description
    assert views[0].ledger["calls"] == []
    assert views[0].ledger["views"][0]["suggestions"][0]["offset"] == 1
    assert views[1].ledger["views"][0]["suggestions"][0]["offset"] > 1
    for view, reply in zip(views, replies, strict=True):
        assert reply.startswith(original + "\n")
        assert "return helper()" not in reply
        assert len(view.encoder.encode(reply[len(original) :])) <= 512
        assert view.ledger["views"][0]["original"] == original
        assert all(
            set(hint) == {"file_path", "offset", "limit"}
            for hint in view.ledger["views"][0]["suggestions"]
        )
        predictions = []
        for text in (original, reply):
            messages = [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "g",
                            "function": {"name": "grep", "arguments": json.dumps(args)},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "g", "content": text},
            ]
            predictions.append(
                native.contextbench_prediction(
                    instance_id="case", repo_path=repo, messages=messages
                )["traj_data"]["pred_spans"]
            )
        assert predictions[0] == predictions[1]


def test_hint_overflow_preserves_original_without_a_decision(monkeypatch, source):
    repo, _ = source
    monkeypatch.setattr(
        grep.OpenRouterDecisions,
        "decide",
        lambda *a, **k: pytest.fail("No room for hints; unexpected billing"),
    )
    view = grep.GrepSourceView(repo=repo, query="issue", policy="jev-hints")
    original = "native reply" * 1600
    assert view.wrap(lambda **kw: original)(pattern="marker") == original
    assert view.ledger["calls"] == []


def test_non_content_errors_and_confined_paths_never_call_jev(monkeypatch, source):
    repo, execute = source
    monkeypatch.setattr(
        grep.OpenRouterDecisions,
        "decide",
        lambda *a, **k: pytest.fail("Unexpected billing"),
    )
    view = grep.GrepSourceView(repo=repo, query="issue", policy="jev")
    wrapped = view.wrap(execute)
    assert wrapped(
        pattern="marker", path=".", output_mode="files_with_matches"
    ) == execute(pattern="marker", path=".", output_mode="files_with_matches")
    with pytest.raises(ValueError, match="escapes benchmark checkout"):
        wrapped(pattern="marker", path="../")
    assert wrapped(pattern="absent_keyword", path=".") == execute(
        pattern="absent_keyword", path="."
    )
    assert view.ledger["calls"] == []


def test_unknown_usage_blocks_concurrent_second_request(monkeypatch, source):
    repo, execute = source
    entered, release, second_started = (threading.Event() for _ in range(3))
    calls, checkpoints = [], []

    def unavailable(self, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        result = decisions(self, **kwargs)
        return result.model_copy(update={"usage": {}})

    monkeypatch.setattr(grep.OpenRouterDecisions, "decide", unavailable)
    view = grep.GrepSourceView(
        repo=repo,
        query="issue",
        policy="jev",
        checkpoint=lambda data: checkpoints.append(copy.deepcopy(data)),
    )
    wrapped = view.wrap(execute)

    def second():
        second_started.set()
        return wrapped(pattern="marker", path=".")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(wrapped, pattern="marker", path=".")
        assert entered.wait(5)
        later = pool.submit(second)
        assert second_started.wait(5)
        assert checkpoints[-1]["calls"][0]["status"] == "pending"
        release.set()
        with pytest.raises(RuntimeError, match="fallback disabled"):
            first.result(timeout=5)
        with pytest.raises(RuntimeError, match="previously failed"):
            later.result(timeout=5)
    assert len(calls) == 1
    assert view.ledger["unknown_usage_calls"] == 1
    assert view.ledger["failed"]
    assert "reply" not in view.ledger["views"][0]


def test_jev_budget_reserves_before_request(monkeypatch, source):
    repo, execute = source
    monkeypatch.setattr(
        grep.OpenRouterDecisions,
        "decide",
        lambda *a, **k: pytest.fail("Unexpected billing"),
    )
    view = grep.GrepSourceView(
        repo=repo, query="issue", policy="jev", max_cost_usd=0.001
    )
    with pytest.raises(RuntimeError, match="cost guard"):
        view.wrap(execute)(pattern="marker", path=".")
    assert view.ledger["failed"] and view.ledger["calls"] == []


def test_api_route_requires_explicit_first_party_environment(monkeypatch):
    names = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="only ANTHROPIC_API_KEY"):
        claude.api_auth("claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    status = {
        "loggedIn": True,
        "authMethod": "api_key",
        "apiProvider": "firstParty",
        "apiKeySource": "ANTHROPIC_API_KEY",
        "email": "private@example.org",
    }
    monkeypatch.setattr(
        claude.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps(status)),
    )
    assert "email" not in claude.api_auth("claude")
    with pytest.raises(ValueError, match="existing CLI login only"):
        claude.subscription_auth("claude")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://another-provider.invalid")
    with pytest.raises(ValueError, match="only ANTHROPIC_API_KEY"):
        claude.api_auth("claude")
    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    status["authMethod"] = "claude.ai"
    with pytest.raises(ValueError, match="did not confirm"):
        claude.api_auth("claude")


@pytest.mark.parametrize("missing_usage", [False, True])
@pytest.mark.parametrize("policy", ["jev", "jev-hints"])
def test_sdk_observes_ranked_reply_before_next_assistant_turn(
    monkeypatch, source, missing_usage, policy
):
    repo, _ = source

    def decide(self, **kwargs):
        result = decisions(self, **kwargs)
        return result.model_copy(update={"usage": {}}) if missing_usage else result

    monkeypatch.setattr(grep.OpenRouterDecisions, "decide", decide)
    view = grep.GrepSourceView(
        repo=repo, query="target", policy=policy, token_budget=256
    )
    row = {"messages": [], "sdk_events": []}

    def event(kind, **fields):
        return make_dataclass(kind, [(k, object) for k in fields])(**fields)

    async def query(*, prompt, options):
        handlers = dict(options.mcp_servers["source"])
        yield event(
            "SystemMessage",
            subtype="init",
            data={"tools": list(claude.TOOLS), "apiKeySource": "ANTHROPIC_API_KEY"},
        )
        yield event(
            "AssistantMessage",
            content=[
                {
                    "id": "g",
                    "name": "mcp__source__grep",
                    "input": {"pattern": "marker", "path": "."},
                }
            ],
        )
        reply = await handlers["grep"]({"pattern": "marker", "path": "."})
        if missing_usage:
            assert reply["isError"]
        elif policy == "jev-hints":
            text = reply["content"][0]["text"]
            assert text.startswith(view.ledger["views"][0]["original"])
            assert '"file_path": "a.py"' in text
            assert "def target" not in text
        else:
            assert "def target" in reply["content"][0]["text"].splitlines()[0]
        yield event(
            "UserMessage", content=[{"tool_use_id": "g", "content": reply["content"]}]
        )
        assert not missing_usage, "SDK advanced after unknown Jev usage"
        assert row["messages"][-1]["content"] == reply["content"][0]["text"]
        yield event(
            "AssistantMessage", content=[{"text": "Read the selected target next."}]
        )
        yield event(
            "ResultMessage",
            subtype="success",
            is_error=False,
            total_cost_usd=0.01,
            num_turns=2,
            result="done",
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

    def retrieve():
        return claude.retrieve(
            repo=repo,
            query_text="target",
            row=row,
            checkpoint=lambda: None,
            cli="test",
            model="test",
            max_turns=3,
            billing_route="anthropic-api",
            grep_view=view,
        )

    if missing_usage:
        with pytest.raises(RuntimeError, match="stop this SDK stream"):
            asyncio.run(retrieve())
        assert view.ledger["unknown_usage_calls"] == 1
        return
    asyncio.run(retrieve())
    assert row["status"] == "ok"
    observed = native.observed_nodes(row["messages"], repo)
    assert any("def target" in node.content for node in observed) == (policy == "jev")
    assert all(node.file == "a.py" for node in observed)
