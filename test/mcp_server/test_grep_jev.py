# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Source → real rg/chunker → OpenRouter HTTP → CLI/MCP, without paid calls."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import threading
from types import SimpleNamespace

import pytest
import requests

from codenib.agent.runtime.grep_jev import GrepJevConfig, GrepJevError, GrepJevRetriever
from codenib.mcp.grep_jev import explore_repository
from codenib.source_fingerprint import capture_repository_source


@pytest.fixture
def repo(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is required for the grep route")
    root = tmp_path / "repository"
    (root / "src").mkdir(parents=True)
    (root / "src" / "service.py").write_text(
        "def retry_request():\n    return 'retry network request'\n\n"
        "def unrelated():\n    return 'other behavior'\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def api(monkeypatch):
    # A missing-key test must never inspect the developer's real OS vault.
    monkeypatch.setattr("codenib.openrouter_auth._os_keyring", lambda: None)
    monkeypatch.setattr("codenib.openrouter_auth._read_file_key", lambda: None)
    state = SimpleNamespace(
        calls=[],
        actions=[{"pattern": "retry", "glob": "**/*.py", "case_sensitive": False}],
        planning_cost=0.01,
        scoring_cost=0.001,
        scores={},
        status=200,
        failure_stage="planning",
        body_override=None,
        hook=lambda stage, payload: None,
    )

    def send(_session, request, **kwargs):
        payload = json.loads(request.body)
        stage = "planning" if request.url.endswith("/chat/completions") else "scoring"
        assert request.url in {
            "https://openrouter.ai/api/v1/chat/completions",
            "https://openrouter.ai/api/alpha/decisions",
        }
        assert request.headers["Authorization"] == "Bearer local-test-key"
        assert kwargs["allow_redirects"] is False
        state.calls.append((stage, payload, kwargs))
        state.hook(stage, payload)
        if stage == "planning":
            body = {
                "model": "anthropic/claude-sonnet-4.6",
                "usage": {"cost": state.planning_cost, "prompt_tokens": 100},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"actions": state.actions})},
                    }
                ],
            }
        else:
            answers = {}
            for name, question in payload["questions"].items():
                node = payload["state"]["candidates"][name]
                score = state.scores.get(node["name"], 3.0)
                answers[name] = {
                    "type": "score",
                    "score": score,
                    "confidence": 1,
                    "probabilities": {
                        "0": 1 - score / 3,
                        "1": 0,
                        "2": 0,
                        "3": score / 3,
                    },
                    "legend": {str(i): c for i, c in enumerate(question["criteria"])},
                }
            body = {
                "model": "typesafe/jev-1.13-20260917",
                "answers": answers,
                "usage": {"cost": state.scoring_cost, "input_tokens": 50},
            }
        response = requests.Response()
        response.url = request.url
        response.status_code = state.status if stage == state.failure_stage else 200
        if state.body_override is not None and stage == state.failure_stage:
            body = state.body_override
        response._content = json.dumps(body).encode()
        response._content_consumed = True
        return response

    monkeypatch.setenv("OPENROUTER_API_KEY", "local-test-key")
    monkeypatch.setattr(requests.Session, "send", send)
    return state


def search(repo, **kwargs):
    config = kwargs.pop("config", GrepJevConfig())
    with capture_repository_source(repo) as source:
        return GrepJevRetriever(config).search(
            source, "Where is retry handled?", **kwargs
        )


def test_shared_key_source_locations_and_actual_usage(repo, api):
    result = search(repo)
    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert node.file == "src/service.py"
    assert (node.start_line, node.end_line) == (0, 1)
    assert "retry_request" in node.content
    assert node.score == 1
    assert result.plan["reported_cost_usd"] == pytest.approx(0.011)
    assert result.plan["provider_calls"][1]["usage"]["input_tokens"] == 50
    assert result.plan["provider_calls"][1]["model"] == "typesafe/jev-1.13-20260917"
    assert result.plan["graph"] is None
    planning = api.calls[0][1]
    assert planning["provider"]["require_parameters"] is True
    overview = json.loads(planning["messages"][1]["content"])
    assert overview["source_file_count"] == 1
    assert "def retry" not in json.dumps(overview)
    assert "local-test-key" not in repr(result)
    assert not (repo / ".codenib").exists()


def test_cli_delivers_verified_one_based_context_without_index(repo, api, capsys):
    from codenib.cli import run

    assert run(["explore", str(repo), "find retry"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"]["verified"] is True
    assert result["source"]["commit_verified"] is False
    assert result["source"]["verification_scope"] == "content-bytes"
    assert result["files"][0]["file"] == "src/service.py"
    context = result["files"][0]["contexts"][0]
    assert context["start_line"] == 1
    assert context["verified"] is True
    assert "retry_request" in context["content"]
    assert result["plan"]["retrieval"]["name"] == "grep_jev"
    assert not (repo / ".codenib").exists()


def test_default_filters_keep_test_and_vendor_source_out_of_both_calls(repo, api):
    for directory in ("tests", "node_modules", "vendor", ".git"):
        (repo / directory).mkdir()
        (repo / directory / "secret.py").write_text(
            "def retry_hidden():\n    return 'excluded-secret'\n"
        )
    (repo / "src" / "test_service.py").write_text(
        "def retry_test():\n    return 'test-only-secret'\n"
    )
    result = search(repo)
    assert all(node.file == "src/service.py" for node in result.nodes)
    assert "excluded-secret" not in json.dumps(api.calls)
    assert "test-only-secret" not in json.dumps(api.calls)


def test_tests_can_be_included_explicitly(repo, api):
    (repo / "test_service.py").write_text("def retry_test():\n    return None\n")
    result = search(repo, config=GrepJevConfig(include_tests=True))
    assert {node.file for node in result.nodes} == {"test_service.py", "src/service.py"}


def test_persisted_exclusions_apply_before_directory_overview_or_source_upload(
    repo, api, monkeypatch, tmp_path
):
    from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
    from codenib.paths import repo_index_dir
    from codenib.repository_source_selection import RepositorySourceSelection

    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    (repo / "private").mkdir()
    (repo / "private" / "key.py").write_text(
        "def retry_secret():\n    return 'private-marker'\n"
    )
    manifest_path = repo_index_dir(repo) / MANIFEST_FILENAME
    manifest_path.parent.mkdir(parents=True)
    RepoManifest(
        repo_path=str(repo), source_selection=RepositorySourceSelection(("private",))
    ).save(manifest_path)
    result = explore_repository(repo, GrepJevConfig(), "retry")
    assert result["files"]
    assert "private-marker" not in json.dumps(api.calls)
    assert "private/" not in json.dumps(api.calls)


def test_escaping_symlink_rejects_source_before_any_provider_call(repo, api, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def retry_secret():\n    return 'outside-marker'\n")
    (repo / "src" / "alias.py").symlink_to(outside)
    with pytest.raises((OSError, RuntimeError, ValueError), match="link target"):
        search(repo)
    assert api.calls == []


def test_internal_symlink_is_not_a_duplicate_source_candidate(repo, api):
    (repo / "src" / "alias.py").symlink_to("service.py")
    result = search(repo)
    assert [node.file for node in result.nodes] == ["src/service.py"]


def test_rg_ignores_config_and_does_not_evaluate_shell_text(
    repo, api, monkeypatch, tmp_path
):
    config = tmp_path / "rg-config"
    config.write_text("--files\n")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    sentinel = repo / "injected"
    api.actions = [
        {
            "pattern": f"retry|$(touch {sentinel})",
            "glob": "**/*.py",
            "case_sensitive": False,
        }
    ]
    assert search(repo).nodes
    assert not sentinel.exists()


def test_visible_prefix_controls_match_range(repo, api):
    (repo / "src" / "service.py").write_text(
        "def long_function():\n" + "    value = '" + "a" * 3200 + "'\n"
        "    return 'retry unseen tail'\n"
    )
    result = search(repo)
    assert result.nodes == []
    assert [stage for stage, _, _ in api.calls] == ["planning"]


def test_synthetic_method_class_line_is_not_a_visible_source_line(repo, api):
    (repo / "src" / "service.py").write_text(
        "class Client:\n    def method(self):\n        value = '"
        + "a" * 3200
        + "'\n        marker = 1\n        return 'retry hidden tail'\n"
    )
    assert search(repo).nodes == []


def test_candidate_cap_batches_and_stable_round_robin_order(repo, api):
    (repo / "src" / "service.py").unlink()
    for index in range(120):
        (repo / "src" / f"file_{index:03}.py").write_text(
            f"def retry_{index}():\n    return 'retry'\n"
        )
    api.actions *= 3
    result = search(repo, top_k=100)
    assert len(result.nodes) == 100
    assert result.plan["candidate_count"] == 100
    assert len(api.calls) == 11
    assert [node.file for node in result.nodes] == [
        f"src/file_{i:03}.py" for i in range(100)
    ]
    assert all(len(call[1]["questions"]) == 10 for call in api.calls[1:])


@pytest.mark.parametrize("status", [401, 402, 429, 503])
@pytest.mark.parametrize("stage", ["planning", "scoring"])
def test_http_errors_never_retry_or_fall_back(repo, api, status, stage):
    api.status, api.failure_stage = status, stage
    api.body_override = {"error": "secret provider body local-test-key"}
    with pytest.raises(GrepJevError) as error:
        search(repo)
    assert "local-test-key" not in str(error.value)
    assert len(api.calls) == (1 if stage == "planning" else 2)


@pytest.mark.parametrize("body", [[], {}, {"model": []}, "local-test-key"])
def test_malformed_planning_responses_are_safe_errors(repo, api, body):
    api.body_override = body
    with pytest.raises(GrepJevError):
        search(repo)
    assert len(api.calls) == 1


@pytest.mark.parametrize(
    "actions",
    [
        [],
        [{"command": "rm -rf /"}],
        [{"pattern": "x", "glob": "**/*", "case_sensitive": "false"}],
        [{"pattern": "x" * 257, "glob": "**/*", "case_sensitive": False}],
    ],
)
def test_unsupported_or_unbounded_plans_never_reach_rg_or_scoring(repo, api, actions):
    api.actions = actions
    with pytest.raises(GrepJevError, match="invalid grep plan"):
        search(repo)
    assert len(api.calls) == 1


@pytest.mark.parametrize("cost", [None, True, -1, float("inf"), float("nan")])
def test_missing_or_invalid_usage_prevents_further_billing(repo, api, cost):
    api.planning_cost = cost
    with pytest.raises(GrepJevError, match="valid cost"):
        search(repo)
    assert len(api.calls) == 1


def test_reported_budget_stops_after_planning_without_scoring(repo, api):
    api.planning_cost = 0.11
    with pytest.raises(GrepJevError, match="limit reached"):
        search(repo)
    assert len(api.calls) == 1


def test_failure_retains_known_cost_and_marks_unreported_in_flight_cost(repo, api):
    api.status, api.failure_stage = 503, "scoring"
    result = explore_repository(repo, GrepJevConfig(), "retry")
    plan = result["plan"]["retrieval"]
    assert plan["status"] == "failed"
    assert plan["reported_cost_usd"] == 0.01
    assert plan["unreported_call_cost"] is True
    assert [call["stage"] for call in plan["provider_calls"]] == ["planning"]


def test_failed_cli_query_returns_nonzero_and_a_safe_diagnostic(repo, api, capsys):
    from codenib.cli import run

    api.status = 503
    assert run(["explore", str(repo), "find retry"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["files"] == []
    assert any(row["code"] == "retrieval_failed" for row in result["diagnostics"])
    assert len(api.calls) == 1


@pytest.mark.parametrize("stage", ["planning", "scoring"])
def test_mutation_during_provider_call_cannot_publish_or_bill_another_batch(
    repo, api, stage
):
    def mutate(active_stage, _payload):
        if active_stage == stage:
            (repo / "src" / "service.py").write_text("def replaced():\n    pass\n")

    api.hook = mutate
    with pytest.raises((OSError, RuntimeError, ValueError)):
        explore_repository(repo, GrepJevConfig(), "find retry")
    assert len(api.calls) == (1 if stage == "planning" else 2)


def test_cancel_after_planning_prevents_scoring(repo, api):
    cancelled = threading.Event()
    api.hook = lambda _stage, _payload: cancelled.set()

    def check():
        if cancelled.is_set():
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        explore_repository(repo, GrepJevConfig(), "find retry", check_cancelled=check)
    assert len(api.calls) == 1


def test_invalid_input_and_missing_key_fail_before_reading_source(
    tmp_path, api, monkeypatch
):
    missing = tmp_path / "absent"
    with pytest.raises(ValueError, match="query"):
        explore_repository(missing, GrepJevConfig(), " ")
    with pytest.raises(ValueError, match="top_k"):
        explore_repository(missing, GrepJevConfig(), "retry", top_k=21)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(GrepJevError, match="OPENROUTER_API_KEY"):
        explore_repository(missing, GrepJevConfig(), "retry")
    assert api.calls == []


def test_mcp_refreshes_source_each_call_and_exposes_only_explore(
    repo, api, monkeypatch
):
    from mcp import Client

    from codenib.mcp import server

    original_surface = server.mcp.tool_surface
    monkeypatch.setattr(server, "_ctx", None)
    server.init_grep_jev_server(repo, GrepJevConfig())

    async def run():
        async with Client(server.mcp, mode="auto", cache=None) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["explore_context"]
            first = await client.call_tool("explore_context", {"query": "retry"})
            assert not first.is_error
            first_body = json.loads(first.content[0].text)
            path = repo / "src" / "service.py"
            path.write_text("def retry_new():\n    return 'new version'\n")
            second = await client.call_tool("explore_context", {"query": "retry"})
            assert not second.is_error
            second_body = json.loads(second.content[0].text)
            assert (
                first_body["source"]["source_fingerprint"]
                != second_body["source"]["source_fingerprint"]
            )
            assert "retry_new" in json.dumps(second_body["files"])
            assert second_body["summary"]["session_call"] == 2

    try:
        asyncio.run(run())
    finally:
        server._ctx.close()
        server.configure_tool_surface(original_surface)


def test_mcp_cancellation_retains_worker_until_in_flight_call_finishes(
    repo, api, monkeypatch
):
    from codenib.mcp import server

    started, release = threading.Event(), threading.Event()
    original_surface = server.mcp.tool_surface
    monkeypatch.setattr(server, "_ctx", None)
    server.init_grep_jev_server(repo, GrepJevConfig())

    def block(stage, _payload):
        if stage == "planning":
            started.set()
            assert release.wait(timeout=5)

    api.hook = block

    async def run():
        task = asyncio.create_task(server.explore_context("retry"))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        runtime = server._ctx.explore_runtime
        assert runtime.pending_worker is not None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await runtime.pending_worker
        assert runtime.ledger.stats()["calls"] == 0
        assert len(api.calls) == 1

    try:
        asyncio.run(run())
    finally:
        release.set()
        server._ctx.close()
        server.configure_tool_surface(original_surface)


@pytest.mark.parametrize("cancel_replacement", [False, True])
def test_replacement_waits_for_cancelled_worker_without_inheriting_its_cancellation(
    repo, api, monkeypatch, cancel_replacement
):
    from codenib.mcp import server

    started, release = threading.Event(), threading.Event()
    original_surface = server.mcp.tool_surface
    monkeypatch.setattr(server, "_ctx", None)
    server.init_grep_jev_server(repo, GrepJevConfig())

    def block(stage, _payload):
        if stage == "planning" and not started.is_set():
            started.set()
            assert release.wait(timeout=5)

    api.hook = block

    async def run():
        waiting = asyncio.Event()
        original_wait = server._wait_for_abandoned_explore_worker

        async def notify_wait(runtime):
            if runtime.pending_worker is not None:
                waiting.set()
            await original_wait(runtime)

        monkeypatch.setattr(server, "_wait_for_abandoned_explore_worker", notify_wait)
        first = asyncio.create_task(server.explore_context("retry"))
        assert await asyncio.to_thread(started.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        runtime = server._ctx.explore_runtime
        abandoned = runtime.pending_worker
        replacement = asyncio.create_task(server.explore_context("retry again"))
        await asyncio.wait_for(waiting.wait(), 5)
        assert not abandoned.done()
        if cancel_replacement:
            replacement.cancel()
            with pytest.raises(asyncio.CancelledError):
                await replacement
            assert not abandoned.done()
            replacement = asyncio.create_task(server.explore_context("retry once more"))
        release.set()
        result = await asyncio.wait_for(replacement, 5)
        assert result["plan"]["retrieval"]["name"] == "grep_jev"
        assert runtime.pending_worker is None
        assert runtime.ledger.stats()["calls"] == 1
        assert [stage for stage, *_ in api.calls] == ["planning", "planning", "scoring"]

    try:
        asyncio.run(run())
    finally:
        release.set()
        server._ctx.close()
        server.configure_tool_surface(original_surface)


@pytest.mark.parametrize(
    "limit", ["MAX_MATERIALIZED_CHUNKS", "MAX_MATERIALIZED_CONTENT_CHARS"]
)
def test_materialized_corpus_limit_stops_before_any_model_call(
    repo, api, monkeypatch, limit
):
    from codenib.agent.runtime import grep_jev as runtime

    monkeypatch.setattr(runtime, limit, 1)
    with capture_repository_source(repo) as source:
        with pytest.raises(GrepJevError, match="chunk materialization limit"):
            GrepJevRetriever().search(source, "retry")
    assert api.calls == []


def test_rg_paths_keep_posix_backslashes_and_normalize_windows_separators():
    from codenib.agent.runtime.grep_jev import _rg_path

    assert _rg_path({"text": r".\src\service.py"}, separator="\\") == "src/service.py"
    assert _rg_path({"text": r"./src/a\b.py"}, separator="/") == r"src/a\b.py"
    raw = b"./src/\xff.py"
    assert os.fsencode(_rg_path({"bytes": base64.b64encode(raw).decode()})) == raw[2:]
    for path in (r"..\secret.py", r"C:\secret.py", r"\\server\secret.py"):
        with pytest.raises(GrepJevError, match="invalid source path"):
            _rg_path({"text": path}, separator="\\")


def test_nul_regex_does_not_abort_other_actions_on_the_selected_text(repo, api):
    api.actions.insert(
        0, {"pattern": r"\x00", "glob": "**/*.py", "case_sensitive": True}
    )
    with capture_repository_source(repo) as source:
        result = GrepJevRetriever().search(source, "retry")
    assert result.nodes
    assert result.plan["actions"][0]["match_lines"] == 0
    assert result.plan["actions"][1]["chunks"] > 0
    assert [stage for stage, *_ in api.calls] == ["planning", "scoring"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX byte filenames")
def test_real_rg_decodes_non_utf8_filenames(tmp_path):
    from codenib.agent.runtime.grep_jev import GrepAction, _RequestBudget, _rg_lines

    if shutil.which("rg") is None:
        pytest.skip("ripgrep is required")
    (tmp_path / os.fsdecode(b"\xff.py")).write_text("def retry(): pass\n")
    matches, truncated = _rg_lines(
        tmp_path,
        GrepAction(pattern="retry", glob="**/*.py", case_sensitive=True),
        _RequestBudget(GrepJevConfig(), lambda: None),
    )
    assert [(os.fsencode(path), line) for path, line in matches] == [(b"\xff.py", 0)]
    assert not truncated
