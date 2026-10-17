"""Native Multi boundaries without Docker, credentials or model calls."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import contextbench_multiswe as multi


@pytest.mark.parametrize(
    "history,artifacts,valid",
    [
        ("1\nselected\n", False, True),
        ("1\n", False, True),
        ("2\nselected\n", False, False),
        ("1\nold-reference\nselected\n", False, False),
        ("1\norigin\nselected\n", False, False),
        ("1\nselected\n", True, False),
    ],
)
def test_multi_solver_rejects_reference_files_and_history(history, artifacts, valid):
    def execute(command):
        if "rev-list" in command:
            return {"output": history, "returncode": 0}
        assert all(path in command for path in multi.REFERENCE_PATHS)
        return {"output": "", "returncode": int(artifacts)}

    environment = SimpleNamespace(execute=execute)
    if valid:
        assert multi.validate_source_only_image(environment, "selected")[
            "reference_artifacts_absent"
        ]
    else:
        with pytest.raises(ValueError, match="source commit|reference artifacts"):
            multi.validate_source_only_image(environment, "selected")


def test_multi_task_identity_and_source_remain_bound(tmp_path):
    row = {
        "instance_id": "fmtlib__fmt-2394",
        "org": "fmtlib",
        "repo": "fmt",
        "number": 2394,
        "base": {"sha": "base"},
    }
    path = tmp_path / "case.jsonl"
    path.write_text(json.dumps(row) + "\n")
    solver = {"original_inst_id": row["instance_id"]}
    config = {"source": "Multi", "repo": "fmtlib/fmt", "source_commit": "base"}
    assert multi.selected_native_case(path, solver, config) == row
    for field, value in (("source_commit", "wrong"), ("repo", "other/repo")):
        with pytest.raises(ValueError, match="source snapshot"):
            multi.selected_native_case(path, solver, {**config, field: value})
    path.write_text((json.dumps(row) + "\n") * 2)
    with pytest.raises(ValueError, match="ambiguous"):
        multi.selected_native_case(path, solver, config)
    assert multi.solver_cwd("Multi", "fmtlib/fmt") == "/home/fmt"
    with pytest.raises(ValueError, match="fmt pilot only"):
        multi.solver_cwd("Multi", "other/repo")


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "timeout",
        "startup_timeout",
        "cleanup_timeout",
        "copy_mismatch",
        "wrong_tree",
    ],
)
def test_native_grader_replaces_reference_before_running_and_removes_owner(
    monkeypatch, tmp_path, failure
):
    solver_run = tmp_path / "solver"
    solver_run.mkdir()
    patch = "exact solver patch\n"
    row = {
        "instance_id": "context-id",
        "original_inst_id": "fmtlib__fmt-2394",
        "model_patch": patch,
    }
    config = {
        "source": "Multi",
        "repo": "fmtlib/fmt",
        "source_commit": "base",
        "source_tree": "tree",
        "image_id": "sha256:source-only",
        "implementation": {},
    }
    (solver_run / "config.json").write_text(json.dumps(config))
    (solver_run / "solver.json").write_text(json.dumps(row))
    (solver_run / "predictions.json").write_text(
        json.dumps([{"instance_id": row["original_inst_id"], "model_patch": patch}])
    )
    dataset = tmp_path / "native.jsonl"
    case = {
        "instance_id": row["original_inst_id"],
        "org": "fmtlib",
        "repo": "fmt",
        "number": 2394,
        "base": {"sha": "base"},
        "fix_patch": "reference fix",
        "test_patch": "native tests",
    }
    dataset.write_text(json.dumps(case) + "\n")
    output = tmp_path / "grade"
    args = SimpleNamespace(
        solver_run=solver_run,
        dataset=dataset,
        output=output,
        timeout=600,
        native_image="sha256:" + "a" * 64,
    )
    monkeypatch.setattr(multi, "native_environment", lambda: multi.HARNESS_HASHES)
    state = {"copied": False, "executed": False, "removed": False}

    def command(cmd, **kwargs):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": args.native_image}])
        if cmd[:2] == ["docker", "run"]:
            assert "--rm" in cmd and "none" in cmd and "--volume" not in cmd
            state["container"] = cmd[cmd.index("--name") + 1]
            if failure == "startup_timeout":
                raise subprocess.TimeoutExpired(cmd, 60)
            return state["container"]
        assert state["container"] in cmd or any(
            item.startswith(state["container"] + ":") for item in cmd
        )
        if cmd[:2] == ["docker", "cp"]:
            assert not state["executed"]
            assert (output / "prediction.patch").read_text() == patch
            assert cmd[-1].endswith(":/home/fix.patch")
            state["copied"] = True
            return ""
        if "sha256sum" in cmd:
            if cmd[-1] == "/home/test.patch":
                content = case["test_patch"]
            elif not state["copied"]:
                content = case["fix_patch"]
            else:
                content = "still reference" if failure == "copy_mismatch" else patch
            return hashlib.sha256(content.encode()).hexdigest() + "  patch"
        return "base\nwrong" if failure == "wrong_tree" else "base\ntree"

    def run(cmd, **kwargs):
        if cmd[:3] == ["docker", "rm", "-f"]:
            assert cmd[-1] == state["container"]
            state["removed"] = True
            assert not json.loads((output / "resolution.json").read_text()).get(
                "container_removed"
            )
            if failure == "cleanup_timeout":
                state["removed"] = False
                raise subprocess.TimeoutExpired(cmd, 60)
        elif cmd[0] == "docker":
            assert state["copied"] and cmd[-1] == "/home/fix-run.sh"
            state["executed"] = True
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd, 600)
            kwargs["stdout"].write("official native test output\n")
        else:
            assert state["executed"] and "multi_swe_bench.harness.gen_report" in cmd
            (output / "grade").mkdir()
            (output / "grade/final_report.json").write_text(
                json.dumps(
                    {
                        "total_instances": 1,
                        "resolved_instances": 1,
                        "completed_instances": 1,
                        "error_instances": 0,
                        "submitted_ids": ["fmtlib/fmt:pr-2394"],
                    }
                )
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(multi, "command", command)
    monkeypatch.setattr(multi.subprocess, "run", run)
    multi.grade_multi(args)
    result = json.loads((output / "resolution.json").read_text())
    assert (
        result["container_removed"]
        == state["removed"]
        == (failure != "cleanup_timeout")
    )
    assert result["resolved"] == (failure is None)
    assert result["status"] == ("graded" if failure is None else "error")
    assert result["model_calls"] == 0
    if failure in {"startup_timeout", "copy_mismatch", "wrong_tree"}:
        assert not state["executed"]
