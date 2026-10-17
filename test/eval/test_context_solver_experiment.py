"""Solver model isolation and metering without live inference or containers."""

import dataclasses
import json
import sys
from types import SimpleNamespace

import pytest

from scripts.benchmark_context_solver import (
    SubscriptionModel,
    delivered_input,
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
    case = SimpleNamespace(source="Multi")
    monkeypatch.setattr(native, "load_inputs", lambda path: ({}, {"case": case}))
    with pytest.raises(ValueError, match="Verified only"):
        delivered_input(tmp_path, "case", 16384)
    case.source = "Verified"
    path = tmp_path / "case.json"
    path.write_text(json.dumps({"instance_id": "case", "status": "error"}))
    with pytest.raises(ValueError, match="usable result"):
        delivered_input(tmp_path, "case", 16384)


def test_solver_failure_removes_only_owned_container_before_completion(
    monkeypatch, tmp_path
):
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
    monkeypatch.setattr(solver, "subscription_auth", lambda cli: {})
    monkeypatch.setattr(
        solver,
        "delivered_input",
        lambda *args: (
            SimpleNamespace(
                instance_id="case",
                original_inst_id="native",
                base_commit="base",
                problem_statement="issue",
            ),
            "source",
            "sha",
            "tree",
        ),
    )
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
            self.container_id = "owned-container"

        def execute(self, command):
            return {"output": "synthetic\ntree\n", "returncode": 0}

    class Agent:
        def __init__(self, *args, **kwargs):
            self.messages = []

        def run(self, *args, **kwargs):
            raise RuntimeError("model failed")

    monkeypatch.setitem(
        sys.modules, "minisweagent.agents.default", SimpleNamespace(DefaultAgent=Agent)
    )
    monkeypatch.setitem(
        sys.modules,
        "minisweagent.environments.docker",
        SimpleNamespace(DockerEnvironment=Environment),
    )
    solver.run(
        SimpleNamespace(
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
    )
    assert removed == ["owned-container"]
    assert json.loads((output / "solver.json").read_text())["container_removed"]
