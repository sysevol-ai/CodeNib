"""Solver model isolation and metering without live inference or containers."""

import dataclasses
import json
import sys
from types import SimpleNamespace

import pytest

from scripts.benchmark_context_solver import (
    SubscriptionModel,
    delivered_input,
    model_observations,
    request_digest,
    retrieval_notes,
    validate_image_snapshot,
)


def test_no_context_control_rejects_notes_before_loading_inputs():
    from scripts.benchmark_context_solver import run

    with pytest.raises(ValueError, match="cannot receive retrieval notes"):
        run(
            SimpleNamespace(live=True, initial_context="none", retrieval_notes_tokens=1)
        )


def test_retrieval_notes_require_immutable_confirmed_answer_and_exact_prefix(tmp_path):
    from scripts.benchmark_jev_base import digest

    assert retrieval_notes(tmp_path, "missing", 0, "unused") is None
    answer = "Verify compatibility before rejecting a signature. 😀\n" * 200
    row = {
        "status": "ok",
        "answer": answer,
        "sdk_result": {"is_error": False, "result": answer},
        "messages": [{"role": "assistant", "content": answer}],
    }
    path = tmp_path / "case.json"
    path.write_text(json.dumps(row))
    receipt = digest(path)
    notes = retrieval_notes(tmp_path, "case", 64, receipt)
    assert notes["text"] and answer.startswith(notes["text"])
    assert 0 < notes["tokens"] <= 64 and notes["truncated"]
    assert notes["retrieval_sha256"] == receipt
    with pytest.raises(ValueError, match="changed"):
        retrieval_notes(tmp_path, "case", 64, "different receipt")
    for invalid in (
        {**row, "status": "error"},
        {**row, "sdk_result": {"is_error": True, "result": answer}},
        {**row, "sdk_result": {"is_error": False, "result": "different"}},
        {**row, "messages": []},
    ):
        path.write_text(json.dumps(invalid))
        with pytest.raises(ValueError, match="confirmed"):
            retrieval_notes(tmp_path, "case", 64, digest(path))


def test_official_synthetic_image_commit_requires_identical_clean_source():
    result = {"output": "synthetic-commit\nbase-tree\n", "returncode": 0}
    assert validate_image_snapshot(result, "base-tree")["matches_selected_tree"]
    for output in ("commit\nwrong-tree\n", "commit\nbase-tree\n M source.py\n"):
        with pytest.raises(ValueError, match="different or dirty"):
            validate_image_snapshot({**result, "output": output}, "base-tree")


@pytest.mark.parametrize("meter", [0.4, None, -1, float("nan")])
def test_solver_sdk_meter_and_tool_isolation(monkeypatch, tmp_path, meter):
    options = []
    Assistant = dataclasses.make_dataclass("AssistantMessage", ["content", "model"])
    Result = dataclasses.make_dataclass(
        "ResultMessage", ["total_cost_usd", "is_error", "subtype"]
    )

    def configure(**kwargs):
        options.append(kwargs)
        return kwargs

    async def query(**kwargs):
        assert "timestamp" not in kwargs["prompt"]
        assert model.calls[-1]["request"]["prompt"] == kwargs["prompt"]
        assert (
            model.calls[-1]["request"]["system_prompt"] == options[-1]["system_prompt"]
        )
        yield Assistant([{"text": "```bash\npwd\n```"}], "fixed-model")
        yield Result(meter, False, "success")

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(ClaudeAgentOptions=configure, query=query),
    )
    model = SubscriptionModel(
        cli="claude",
        model="fixed-model",
        cwd=tmp_path,
        max_meter_usd=0.5,
        calls=[],
        checkpoint=lambda: None,
    )
    messages = [
        {"role": "system", "content": "Shell format"},
        {"role": "user", "content": "Issue and source", "timestamp": 123},
    ]
    if meter == 0.4:
        assert model.query(messages)["content"].startswith("```bash")
        assert model.cost == 0.4
        model.query(messages)
        assert options[-1]["max_budget_usd"] == pytest.approx(0.1)
        with pytest.raises(RuntimeError, match="exhausted"):
            model.query(messages)
        assert model.n_calls == 2
        assert model.budget_exhausted
    else:
        with pytest.raises(ValueError, match="metering"):
            model.query(messages)
        assert not model.budget_exhausted
    assert options[0]["tools"] == options[0]["allowed_tools"] == []
    assert options[0]["mcp_servers"] == {} and options[0]["strict_mcp_config"]
    assert options[0]["setting_sources"] == []
    assert model.calls[0]["input_delivery_confirmed"]
    observed = model_observations({"instance_id": "case", "calls": model.calls})
    assert observed["confirmed_inputs"][0]["messages"] == [
        {"role": "system", "content": "Shell format"},
        {"role": "user", "content": "Issue and source"},
    ]


@pytest.mark.parametrize("key_source", ["ANTHROPIC_API_KEY", "none"])
@pytest.mark.parametrize("actual_model", ["claude-haiku-5-5", "unexpected-model"])
def test_api_solver_requires_actual_sdk_key_route(
    monkeypatch, tmp_path, key_source, actual_model
):
    def event(kind, **fields):
        return dataclasses.make_dataclass(kind, [(k, object) for k in fields])(**fields)

    async def query(**kwargs):
        yield event(
            "SystemMessage",
            subtype="init",
            data={"tools": [], "apiKeySource": key_source},
        )
        yield event(
            "AssistantMessage",
            content=[{"text": "```bash\npwd\n```"}],
            model=actual_model,
        )
        yield event(
            "ResultMessage", total_cost_usd=0.001, is_error=False, subtype="success"
        )

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(ClaudeAgentOptions=lambda **kwargs: kwargs, query=query),
    )
    model = SubscriptionModel(
        cli="claude",
        model="claude-haiku-5-5",
        cwd=tmp_path,
        max_meter_usd=0.1,
        calls=[],
        checkpoint=lambda: None,
        billing_route="anthropic-api",
    )
    messages = [
        {"role": "system", "content": "Shell format"},
        {"role": "user", "content": "Issue"},
    ]
    if key_source == "none":
        with pytest.raises(ValueError, match="API-key route"):
            model.query(messages)
        assert "sdk_reported_cost_usd" not in model.calls[0]
        assert not model.calls[0]["input_delivery_confirmed"]
    elif actual_model == "unexpected-model":
        with pytest.raises(ValueError, match="configured model"):
            model.query(messages)
        assert model.cost == model.calls[0]["sdk_reported_cost_usd"] == 0.001
    else:
        assert model.query(messages)["content"].startswith("```bash")
        assert model.cost == 0.001
        assert model.calls[0]["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize(
    "result_error,stream_error", [(True, True), (False, True), (True, False)]
)
def test_solver_retains_terminal_meter_when_stream_fails(
    monkeypatch, tmp_path, result_error, stream_error
):
    Assistant = dataclasses.make_dataclass("AssistantMessage", ["content", "model"])
    Result = dataclasses.make_dataclass(
        "ResultMessage", ["total_cost_usd", "is_error", "subtype"]
    )

    async def query(**kwargs):
        yield Assistant([{"text": "```bash\npwd\n```"}], "fixed-model")
        yield Result(0.4, result_error, "error_max_budget_usd")
        if stream_error:
            # The real SDK yields the budget result, then raises on iteration.
            raise RuntimeError("SDK iterator failed after terminal result")

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(ClaudeAgentOptions=lambda **kwargs: kwargs, query=query),
    )
    checkpoints = []
    model = SubscriptionModel(
        cli="claude",
        model="fixed-model",
        cwd=tmp_path,
        max_meter_usd=0.3,
        calls=[],
        checkpoint=lambda: checkpoints.append([dict(call) for call in model.calls]),
    )
    messages = [
        {"role": "system", "content": "Shell format"},
        {"role": "user", "content": "Issue and source"},
    ]
    error = "SDK iterator failed" if stream_error else "Solver model failed"
    with pytest.raises(RuntimeError, match=error):
        model.query(messages)
    assert model.cost == 0.4
    assert checkpoints[-1][0]["sdk_reported_cost_usd"] == 0.4
    assert model.calls[0]["input_delivery_confirmed"]
    assert model.budget_exhausted is result_error
    with pytest.raises(RuntimeError, match="SDK meter exhausted"):
        model.query(messages)
    assert model.n_calls == len(model.calls) == 1


def test_solver_observations_exclude_unconfirmed_and_unrequested_output():
    from scripts.benchmark_context_solver import _HISTORY_PREFIX

    request = {
        "system_prompt": "Shell format",
        "prompt": _HISTORY_PREFIX
        + json.dumps(
            [
                {"role": "user", "content": "Issue and source"},
                {"role": "assistant", "content": "```bash\ncat large.py\n```"},
                {
                    "role": "user",
                    "content": (
                        "<output_head>visible head</output_head>"
                        "<elided_chars>5000</elided_chars>"
                        "<output_tail>visible tail</output_tail>"
                    ),
                },
            ]
        ),
    }
    first = {
        "turn": 1,
        "request": request,
        "request_sha256": request_digest(request),
        "input_delivery_confirmed": True,
    }
    row = {
        "instance_id": "case",
        "calls": [
            first,
            {**first, "turn": 2, "input_delivery_confirmed": False},
            {"turn": 3},
        ],
        "messages": [
            {"role": "user", "content": "Final submission output never delivered"}
        ],
    }
    result = model_observations(row)
    assert len(result["confirmed_inputs"]) == 1
    assert result["unconfirmed_inputs"] == [2] and result["missing_input_receipts"] == [
        3
    ]
    assert not result["all_model_inputs_confirmed"]
    assert "Final submission" not in json.dumps(result)
    assert "visible head" in json.dumps(result) and "visible tail" in json.dumps(result)
    first["request"]["prompt"] += "tampered"
    with pytest.raises(ValueError, match="receipt changed"):
        model_observations(row)


@pytest.mark.integration
def test_native_loop_records_rendered_inputs_without_elided_or_terminal_output(
    monkeypatch, tmp_path
):
    import os
    import subprocess
    from pathlib import Path

    import yaml

    from scripts.benchmark_context_solver import CONTEXTBENCH_UPSTREAM_REVISION

    checkout = os.environ.get("CONTEXTBENCH_CHECKOUT")
    if not checkout:
        pytest.skip("Set CONTEXTBENCH_CHECKOUT to the pinned external checkout")
    upstream = Path(checkout)
    assert (
        subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
        ).strip()
        == CONTEXTBENCH_UPSTREAM_REVISION
    )
    mini = (
        upstream
        / "agent-frameworks/mini-swe-agent/multi-poly-pro-verified/mini-swe-agent/src"
    )
    monkeypatch.syspath_prepend(str(mini))
    from minisweagent.agents.default import DefaultAgent

    config = yaml.safe_load(
        (mini / "minisweagent/config/extra/swebench.yaml").read_text()
    )["agent"]
    config.update(
        system_template="Return one bash action.",
        instance_template="{{task}}",
        step_limit=3,
    )
    Assistant = dataclasses.make_dataclass("AssistantMessage", ["content", "model"])
    Result = dataclasses.make_dataclass(
        "ResultMessage", ["total_cost_usd", "is_error", "subtype"]
    )
    requests = []

    async def query(**kwargs):
        requests.append(kwargs["prompt"])
        command = "cat large.py" if len(requests) == 1 else "submit"
        yield Assistant([{"text": f"```bash\n{command}\n```"}], "fixture-model")
        yield Result(0.01, False, "success")

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            ClaudeAgentOptions=lambda **kwargs: kwargs,
            query=query,
        ),
    )

    class Environment:
        def get_template_vars(self):
            return {}

        def execute(self, command):
            return {
                "returncode": 0,
                "output": (
                    "h" * 6000 + "SOURCE_IN_ELIDED_MIDDLE" + "t" * 6000
                    if command == "cat large.py"
                    else "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nUNREAD_FINAL_DIFF"
                ),
            }

    model = SubscriptionModel(
        cli="fixture",
        model="fixture-model",
        cwd=tmp_path,
        max_meter_usd=1,
        calls=[],
        checkpoint=lambda: None,
    )
    agent = DefaultAgent(model, Environment(), **config)
    status, answer = agent.run("Inspect a large file")
    assert status == "Submitted" and answer == "UNREAD_FINAL_DIFF"
    result = model_observations({"instance_id": "case", "calls": model.calls})
    assert result["all_model_inputs_confirmed"] and len(result["confirmed_inputs"]) == 2
    delivered = json.dumps(result)
    assert "output_head" in delivered and "output_tail" in delivered
    assert (
        "SOURCE_IN_ELIDED_MIDDLE" not in delivered
        and "UNREAD_FINAL_DIFF" not in delivered
    )
    assert "UNREAD_FINAL_DIFF" in agent.messages[-1]["content"]


def test_delivered_input_rejects_other_native_harnesses_and_failed_retrieval(
    monkeypatch, tmp_path
):
    from scripts import benchmark_context_agent as native
    from scripts.benchmark_jev_base import digest

    prepared = tmp_path / "prepared"
    prepared.mkdir()
    manifest = prepared / "manifest.json"
    manifest.write_text("fixed source")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "prepared": str(prepared),
                "manifest_sha256": digest(manifest),
                "implementation": {},
            }
        )
    )
    case = SimpleNamespace(source="Multi", repo="other/repo")
    monkeypatch.setattr(native, "load_inputs", lambda path: ({}, {"case": case}))
    with pytest.raises(ValueError, match="Unsupported native repository"):
        delivered_input(tmp_path, "case", 16384)
    case.source = "Verified"
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"instance_id": "case", "status": "error"}))
    with pytest.raises(ValueError, match="usable result"):
        delivered_input(tmp_path, "case", 16384)


@pytest.mark.parametrize(
    "source,repo,reference_artifacts,imported_provider_route,failure",
    [
        ("Verified", "owner/repo", False, False, "generic"),
        ("Multi", "fmtlib/fmt", False, False, "generic"),
        ("Multi", "fmtlib/fmt", True, False, "generic"),
        ("Multi", "fmtlib/fmt", False, True, "generic"),
        ("Multi", "google/gson", False, False, "generic"),
        ("Multi", "google/gson", True, False, "generic"),
        ("Multi", "google/gson", False, True, "generic"),
        ("Multi", "ponylang/ponyc", False, False, "generic"),
        ("Multi", "ponylang/ponyc", True, False, "generic"),
        ("Multi", "ponylang/ponyc", False, True, "generic"),
        ("Multi", "ponylang/ponyc", False, False, "budget"),
        ("Multi", "ponylang/ponyc", False, False, "budget_notes"),
        ("Multi", "ponylang/ponyc", False, False, "budget_none"),
        ("Multi", "ponylang/ponyc", False, False, "budget_api"),
        ("Multi", "ponylang/ponyc", False, False, "budget_unknown_usage"),
        ("Multi", "ponylang/ponyc", False, False, "budget_export_failure"),
        ("Multi", "ponylang/ponyc", False, False, "non_budget"),
        ("Verified", "owner/repo", False, False, "budget_pair"),
        ("Verified", "owner/repo", False, False, "budget_pair_none"),
    ],
)
def test_solver_failure_preserves_metered_budget_patch_and_removes_owned_container(
    monkeypatch,
    tmp_path,
    source,
    repo,
    reference_artifacts,
    imported_provider_route,
    failure,
):
    import builtins
    import os

    from scripts import benchmark_context_solver as solver

    mini = (
        tmp_path
        / "agent-frameworks/mini-swe-agent/multi-poly-pro-verified"
        / "mini-swe-agent/src/minisweagent"
    )
    (mini / "config/extra").mkdir(parents=True)
    (mini / "config/extra/swebench.yaml").write_text(
        "agent: {}\nenvironment:\n  env: {}\n"
    )
    output = tmp_path / "result"
    note_text = "Unverified hypothesis.\na.py:999: UNREAD_NOTE_SOURCE\n"
    note_input = tmp_path / "case.json"
    if failure == "budget_notes":
        note_input.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "answer": note_text,
                    "sdk_result": {"is_error": False, "result": note_text},
                    "messages": [{"role": "assistant", "content": note_text}],
                }
            )
        )
    monkeypatch.setattr(solver.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(
        solver.shutil, "disk_usage", lambda path: SimpleNamespace(free=16 * 1024**3)
    )
    original_auth = solver.subscription_auth
    original_import = builtins.__import__
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def native_import(name, *args, **kwargs):
        module = original_import(name, *args, **kwargs)
        if imported_provider_route and name == "minisweagent.environments.docker":
            # Reproduce the native initializer loading credentials from dotenv.
            monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-from-native-dotenv")
        return module

    def effective_auth(cli):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return original_auth(cli)  # Reject before a CLI or model call.
        return {}

    monkeypatch.setattr(builtins, "__import__", native_import)
    monkeypatch.setattr(solver, "subscription_auth", effective_auth)
    api_auth_calls = []

    def effective_api_auth(cli):
        assert os.environ.get("ANTHROPIC_API_KEY") == "fixture-api-only"
        api_auth_calls.append(cli)
        return {"apiKeySource": "ANTHROPIC_API_KEY"}

    monkeypatch.setattr(solver, "api_auth", effective_api_auth)
    if failure == "budget_api":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-api-only")
    monkeypatch.setattr(
        solver,
        "delivered_input",
        lambda *args: (
            SimpleNamespace(
                instance_id="case",
                original_inst_id="native",
                base_commit="base",
                problem_statement="issue",
                source=source,
                repo=repo,
            ),
            "source",
            solver.digest(note_input) if failure == "budget_notes" else "sha",
            "tree",
            tmp_path / "source",
        ),
    )
    monkeypatch.setattr(solver, "initial_source_receipt", lambda *args: None)
    if failure in {"budget_pair", "budget_pair_none"}:
        from hashlib import sha256

        from scripts.contextbench_search_pair_solver import PairHandoff

        pair_handoff = PairHandoff(
            solver.delivered_input(None, None, None)[0],
            tmp_path / "source",
            "tree",
            json.dumps(
                {
                    "source_bodies_included": False,
                    "entries": [
                        {
                            "file": "a.py",
                            "lines": [1, 2],
                            "handle": "versioned-location",
                        }
                    ],
                }
            ),
            {
                "instance_id": "case",
                "case_terminal_sha256": "sha",
                "search_sessions": [
                    {
                        "phase": "search:" + label,
                        "usage_usd": fee,
                        "session_id_sha256": sha256(label.encode()).hexdigest(),
                    }
                    for label, fee in [("first", 0.04), ("scout", 0.06)]
                ],
            },
        )
        monkeypatch.setattr(solver, "load_pair_handoff", lambda *args: pair_handoff)
    monkeypatch.setattr(solver, "freeze_solver", lambda *args: {})
    monkeypatch.setattr(solver.importlib.metadata, "version", lambda name: "fixture")
    monkeypatch.setattr(
        solver.subprocess,
        "check_output",
        lambda command, **kw: (
            solver.CONTEXTBENCH_UPSTREAM_REVISION
            if command[0] == "git"
            else '[{"Id":"sha256:image"}]'
        ),
    )
    removed = []

    def execute(command, **kwargs):
        if command[0] == "docker":
            assert command == ["docker", "rm", "-f", "owned-container"]
            row = json.loads((output / "solver.json").read_text())
            assert row["status"] == "error" and not row.get("container_removed")
            removed.append(command[-1])

    monkeypatch.setattr(solver.subprocess, "run", execute)
    commands = []
    edited = False
    exported_patch = "PATCH_FROM_COMPLETED_EDIT_NOT_DELIVERED_TO_MODEL"

    class Environment:
        def __init__(self, **kwargs):
            assert kwargs["cwd"] == (
                f"/home/{repo.split('/')[1]}" if source == "Multi" else "/testbed"
            )
            self.container_id = "owned-container"

        def execute(self, command):
            nonlocal edited
            commands.append(command)
            if command == "edit source":
                edited = True
                return {"output": "edit done", "returncode": 0}
            if command == "git add -A && git diff --cached --binary":
                assert edited
                return {
                    "output": exported_patch,
                    "returncode": int(failure == "budget_export_failure"),
                }
            if "rev-list" in command:
                return {"output": "1\nsynthetic\n", "returncode": 0}
            if "for path" in command:
                return {"output": "", "returncode": int(reference_artifacts)}
            return {"output": "synthetic\ntree\n", "returncode": 0}

    agent_runs = []
    requests = []
    Assistant = dataclasses.make_dataclass("AssistantMessage", ["content", "model"])
    Result = dataclasses.make_dataclass(
        "ResultMessage", ["total_cost_usd", "is_error", "subtype", "session_id"]
    )

    async def query(**kwargs):
        requests.append(kwargs["prompt"])
        yield dataclasses.make_dataclass("SystemMessage", ["subtype", "data"])(
            "init",
            {
                "tools": [],
                "apiKeySource": (
                    "ANTHROPIC_API_KEY" if failure == "budget_api" else "none"
                ),
            },
        )
        if failure != "budget_api":
            yield dataclasses.make_dataclass("RateLimitEvent", ["rate_limit_info"])(
                {
                    "status": "allowed",
                    "raw": {"overageEnabled": False, "isUsingOverage": False},
                }
            )
        first = len(requests) == 1
        command = "edit source" if first else "UNEXECUTED_FAILED_REPLY"
        yield Assistant([{"text": f"```bash\n{command}\n```"}], "fixed-model")
        if first:
            yield Result(0.1, False, "success", "repair-1")
        else:
            yield Result(
                None if failure == "budget_unknown_usage" else 0.9,
                True,
                (
                    "error_during_execution"
                    if failure == "non_budget"
                    else "error_max_budget_usd"
                ),
                "repair-2",
            )
            raise RuntimeError("SDK iterator failed after terminal result")

    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(ClaudeAgentOptions=lambda **kwargs: kwargs, query=query),
    )

    class Agent:
        def __init__(self, model, environment, **kwargs):
            self.model, self.environment = model, environment
            self.messages = []
            self.config = SimpleNamespace(action_observation_template="output")
            self.instance_template = kwargs["instance_template"]

        def render_template(self, template, **kwargs):
            return kwargs["output"]["output"]

        def run(self, *args, **kwargs):
            from jinja2 import Template

            agent_runs.append(True)
            if failure == "generic":
                raise RuntimeError("model failed")
            self.messages = [
                {"role": "system", "content": "Shell format"},
                {
                    "role": "user",
                    "content": Template(self.instance_template).render(
                        task=args[0], **kwargs
                    ),
                },
            ]
            reply = self.model.query(self.messages)
            self.messages.append({"role": "assistant", "content": reply["content"]})
            observation = self.environment.execute("edit source")
            self.messages.append({"role": "user", "content": observation["output"]})
            self.model.query(self.messages)
            pytest.fail("The budget/error reply must not reach the solver loop")

    monkeypatch.setitem(
        sys.modules, "minisweagent.agents.default", SimpleNamespace(DefaultAgent=Agent)
    )
    monkeypatch.setitem(
        sys.modules,
        "minisweagent.environments.docker",
        SimpleNamespace(DockerEnvironment=Environment),
    )
    args = SimpleNamespace(
        live=True,
        max_turns=10,
        max_meter_usd=1,
        image="sha256:image",
        upstream=tmp_path,
        retrieval=tmp_path,
        instance_id="case",
        budget=16384,
        output=output,
        model="fixed-model",
        retrieval_notes_tokens=128 if failure == "budget_notes" else 0,
        initial_context=(
            "none" if failure in {"budget_none", "budget_pair_none"} else "retrieved"
        ),
        billing_route="anthropic-api" if failure == "budget_api" else "subscription",
    )
    if failure in {"budget_pair", "budget_pair_none"}:
        args.search_pair, args.retrieval = tmp_path, None
        args.total_meter_usd = 1.1
    if imported_provider_route:
        with pytest.raises(ValueError, match="existing CLI login"):
            solver.run(args)
        assert not output.exists() and not removed and not agent_runs
        return
    solver.run(args)
    assert removed == ["owned-container"]
    row = json.loads((output / "solver.json").read_text())
    assert row["status"] == "error" and row["container_removed"]
    assert bool(api_auth_calls) is (failure == "budget_api")
    assert bool(agent_runs) is not reference_artifacts
    predictions = output / "predictions.json"
    if failure == "generic":
        assert not predictions.exists() and not requests
        return
    assert edited and len(requests) == len(row["calls"]) == 2
    assert "UNEXECUTED_FAILED_REPLY" not in commands
    observations = (output / "model-observations.json").read_text()
    assert exported_patch not in observations
    assert "UNEXECUTED_FAILED_REPLY" not in observations
    assert not row.get("native_resolution")
    if failure == "budget_unknown_usage":
        assert row["missing_metering_calls"] == 1
        assert row["sdk_reported_cost_usd"] is None
        assert "metering" in row["error"]
    else:
        assert row["sdk_reported_cost_usd"] == 1.0
        assert row["missing_metering_calls"] == 0
        assert "SDK iterator failed" in row["error"]
    captured = {
        "budget",
        "budget_notes",
        "budget_none",
        "budget_api",
        "budget_pair",
        "budget_pair_none",
    }
    if failure in captured | {"budget_export_failure"}:
        assert row["budget_exhausted"]
        assert row["patch_export_reason"] == "metered_sdk_budget_stop"
        assert commands[-1] == "git add -A && git diff --cached --binary"
    else:
        assert not row.get("budget_exhausted")
        assert commands[-1] == "edit source"
    if failure in captured:
        assert row["model_patch"] == exported_patch
        assert json.loads(predictions.read_text()) == [
            {
                "instance_id": "native",
                "model_name_or_path": "fixed-model",
                "model_patch": exported_patch,
            }
        ]
    else:
        assert not predictions.exists()
    if failure == "budget_export_failure":
        assert "Failed to export" in row["patch_export_error"]
    if failure == "budget_notes":
        assert row["retrieval_notes"]["text"] == note_text
        assert "UNREAD_NOTE_SOURCE" in observations
        assert (
            json.loads(observations)["source_attribution"]["base_source_line_count"]
            == 0
        )
    if failure == "budget_none":
        first = json.loads(observations)["confirmed_inputs"][0]["messages"][1]
        assert "<retrieved_source>\n[]\n</retrieved_source>" in first["content"]
        assert "<retrieved_source>\nsource\n</retrieved_source>" not in first["content"]
        assert row["retrieval_notes"] is None
    if failure in {"budget_pair", "budget_pair_none"}:
        included = failure == "budget_pair"
        usage = json.loads((output / "whole-run-usage.json").read_text())
        assert usage["search_included"] is included
        assert usage["total_model_usage_usd"] == pytest.approx(1.1 if included else 1.0)
        assert usage["native_resolution"] is None and usage["solver_status"] == "error"
        budget = json.loads((output / "config.json").read_text())[
            "whole_run_metering_budget"
        ]
        config = json.loads((output / "config.json").read_text())
        assert config["context_budget"] is None
        assert config["navigation_token_budget"] == (2048 if included else None)
        assert budget["repair_meter_usd"] == pytest.approx(1.0 if included else 1.1)
        first = json.loads(observations)["confirmed_inputs"][0]["messages"][1]
        assert ("<context_navigation>" in first["content"]) is included
        assert (output / "navigation-delivery.json").exists() is included
        if included:
            delivered = json.loads((output / "navigation-delivery.json").read_text())
            assert delivered["confirmed_turns"] == [1, 2]
            assert delivered["source_body_credit"] == 0
