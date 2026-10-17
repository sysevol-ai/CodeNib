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
    validate_image_snapshot,
)


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
    else:
        with pytest.raises(ValueError, match="metering"):
            model.query(messages)
    assert options[0]["tools"] == options[0]["allowed_tools"] == []
    assert options[0]["mcp_servers"] == {} and options[0]["strict_mcp_config"]
    assert options[0]["setting_sources"] == []
    assert model.calls[0]["input_delivery_confirmed"]
    observed = model_observations({"instance_id": "case", "calls": model.calls})
    assert observed["confirmed_inputs"][0]["messages"] == [
        {"role": "system", "content": "Shell format"},
        {"role": "user", "content": "Issue and source"},
    ]


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
    with pytest.raises(ValueError, match="validated Multi"):
        delivered_input(tmp_path, "case", 16384)
    case.source = "Verified"
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"instance_id": "case", "status": "error"}))
    with pytest.raises(ValueError, match="usable result"):
        delivered_input(tmp_path, "case", 16384)


@pytest.mark.parametrize(
    "source,repo,reference_artifacts,imported_provider_route",
    [
        ("Verified", "owner/repo", False, False),
        ("Multi", "fmtlib/fmt", False, False),
        ("Multi", "fmtlib/fmt", True, False),
        ("Multi", "fmtlib/fmt", False, True),
        ("Multi", "google/gson", False, False),
        ("Multi", "google/gson", True, False),
        ("Multi", "google/gson", False, True),
        ("Multi", "ponylang/ponyc", False, False),
        ("Multi", "ponylang/ponyc", True, False),
        ("Multi", "ponylang/ponyc", False, True),
    ],
)
def test_solver_failure_removes_only_owned_container_before_completion(
    monkeypatch, tmp_path, source, repo, reference_artifacts, imported_provider_route
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
            "sha",
            "tree",
            tmp_path,
        ),
    )
    monkeypatch.setattr(solver, "initial_source_receipt", lambda *args: None)
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

    class Environment:
        def __init__(self, **kwargs):
            assert kwargs["cwd"] == (
                f"/home/{repo.split('/')[1]}" if source == "Multi" else "/testbed"
            )
            self.container_id = "owned-container"

        def execute(self, command):
            if "rev-list" in command:
                return {"output": "1\nsynthetic\n", "returncode": 0}
            if "for path" in command:
                return {"output": "", "returncode": int(reference_artifacts)}
            return {"output": "synthetic\ntree\n", "returncode": 0}

    agent_runs = []

    class Agent:
        def __init__(self, *args, **kwargs):
            self.messages = []

        def run(self, *args, **kwargs):
            agent_runs.append(True)
            raise RuntimeError("model failed")

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
    )
    if imported_provider_route:
        with pytest.raises(ValueError, match="existing CLI login"):
            solver.run(args)
        assert not output.exists() and not removed and not agent_runs
        return
    solver.run(args)
    assert removed == ["owned-container"]
    assert json.loads((output / "solver.json").read_text())["container_removed"]
    assert bool(agent_runs) is not reference_artifacts
