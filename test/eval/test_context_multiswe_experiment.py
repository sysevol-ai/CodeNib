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


@pytest.mark.parametrize(
    "repo,number",
    [("fmtlib/fmt", 2394), ("google/gson", 1391), ("ponylang/ponyc", 2205)],
)
def test_multi_task_identity_and_source_remain_bound(tmp_path, repo, number):
    org, name = repo.split("/")
    row = {
        "instance_id": f"{org}__{name}-{number}",
        "org": org,
        "repo": name,
        "number": number,
        "base": {"sha": "base"},
    }
    path = tmp_path / "case.jsonl"
    path.write_text(json.dumps(row) + "\n")
    solver = {"original_inst_id": row["instance_id"]}
    config = {"source": "Multi", "repo": repo, "source_commit": "base"}
    assert multi.selected_native_case(path, solver, config) == row
    for field, value in (
        ("source_commit", "wrong"),
        ("repo", "other/repo"),
        ("repo", "google/gson" if repo == "fmtlib/fmt" else "fmtlib/fmt"),
    ):
        with pytest.raises(ValueError, match="source snapshot"):
            multi.selected_native_case(path, solver, {**config, field: value})
    path.write_text((json.dumps(row) + "\n") * 2)
    with pytest.raises(ValueError, match="ambiguous"):
        multi.selected_native_case(path, solver, config)
    assert multi.solver_cwd("Multi", repo) == f"/home/{name}"
    with pytest.raises(ValueError, match="Unsupported native repository"):
        multi.solver_cwd("Multi", "other/repo")


@pytest.mark.parametrize(
    "overrides,repo,message",
    [
        ({"native_memory_gib": 64}, "ponylang/ponyc", "memory"),
        ({"native_network": "host"}, "ponylang/ponyc", "network"),
        ({"native_test_workers": 0}, "ponylang/ponyc", "workers"),
        ({"native_test_workers": 4}, "ponylang/ponyc", "workers"),
        ({"native_test_workers": 2}, "fmtlib/fmt", "only for Ponyc"),
        ({"native_cpu_count": 8}, "fmtlib/fmt", "CPU count"),
    ],
)
def test_native_resources_reject_before_container_or_output(
    monkeypatch, tmp_path, overrides, repo, message
):
    solver = tmp_path / "solver"
    solver.mkdir()
    (solver / "config.json").write_text(json.dumps({"repo": repo}))
    output = tmp_path / "grade"
    args = SimpleNamespace(
        solver_run=solver,
        output=output,
        timeout=900,
        native_image="sha256:" + "a" * 64,
        **overrides,
    )
    monkeypatch.setattr(multi, "native_environment", lambda: multi.HARNESS_HASHES)

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid resources must not dispatch Docker")

    monkeypatch.setattr(multi, "command", unexpected)
    with pytest.raises(ValueError, match=message):
        multi.grade_multi(args)
    assert not output.exists()


def test_native_affinity_rejects_insufficient_cpus_before_output(monkeypatch, tmp_path):
    monkeypatch.setattr(multi.os, "sched_getaffinity", lambda pid: {7})
    output = tmp_path / "grade"
    args = SimpleNamespace(timeout=900, native_cpu_count=2, output=output)
    with pytest.raises(ValueError, match="at least two allowed CPUs"):
        multi.grade_multi(args)
    assert not output.exists()


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "timeout",
        "startup_timeout",
        "cleanup_timeout",
        "copy_mismatch",
        "wrong_tree",
        "wrong_script",
        "wrong_report_identity",
    ],
)
@pytest.mark.parametrize(
    "repo,number,native_exit_code,native_profile",
    [
        ("fmtlib/fmt", 2394, 0, False),
        ("google/gson", 1391, 1, False),
        ("ponylang/ponyc", 2205, 2, False),
        ("ponylang/ponyc", 2205, 2, True),
    ],
)
def test_native_grader_replaces_reference_before_running_and_removes_owner(
    monkeypatch, tmp_path, failure, repo, number, native_exit_code, native_profile
):
    org, name = repo.split("/")
    solver_run = tmp_path / "solver"
    solver_run.mkdir()
    patch = "exact solver patch\n"
    row = {
        "instance_id": "context-id",
        "original_inst_id": f"{org}__{name}-{number}",
        "model_patch": patch,
    }
    config = {
        "source": "Multi",
        "repo": repo,
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
        "org": org,
        "repo": name,
        "number": number,
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
    if native_profile:
        args.native_memory_gib = 8
        args.native_network = "bridge"
        args.native_test_workers = 2
        args.native_cpu_count = 2
        monkeypatch.setattr(multi.os, "sched_getaffinity", lambda pid: {7, 9, 12})
    monkeypatch.setattr(multi, "native_environment", lambda: multi.HARNESS_HASHES)
    scripts = {
        script: f"pinned native {repo} {script}"
        for script in ("run.sh", "test-run.sh", "fix-run.sh")
    }
    monkeypatch.setattr(
        multi,
        "native_script_hashes",
        lambda case: {
            script: hashlib.sha256(content.encode()).hexdigest()
            for script, content in scripts.items()
        },
    )
    state = {"copied": False, "executed": False, "removed": False}

    def command(cmd, **kwargs):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": args.native_image}])
        if cmd[:2] == ["docker", "run"]:
            assert "--rm" in cmd and "--volume" not in cmd
            assert cmd[cmd.index("--network") + 1] == (
                "bridge" if native_profile else "none"
            )
            assert cmd[cmd.index("--memory") + 1] == ("8g" if native_profile else "3g")
            assert cmd[cmd.index("--cpus") + 1] == "2"
            assert cmd[cmd.index("--pids-limit") + 1] == "256"
            if native_profile:
                assert cmd[cmd.index("--env") + 1] == "MAKEFLAGS=num_cores=2"
                assert cmd[cmd.index("--cpuset-cpus") + 1] == "7,9"
            else:
                assert "--env" not in cmd
                assert "--cpuset-cpus" not in cmd
            recorded = json.loads((output / "config.json").read_text())
            assert recorded["native_resources"] == {
                "memory_gib": 8 if native_profile else 3,
                "cpu_quota": 2,
                "cpu_affinity": "7,9" if native_profile else None,
                "pids_limit": 256,
                "network": "bridge" if native_profile else "none",
                "test_workers": 2 if native_profile else None,
                "environment": {"MAKEFLAGS": "num_cores=2"} if native_profile else {},
            }
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
            if cmd[-1].rsplit("/", 1)[-1] in scripts:
                content = (
                    "wrong script"
                    if failure == "wrong_script"
                    else scripts[cmd[-1].rsplit("/", 1)[-1]]
                )
            elif cmd[-1] == "/home/test.patch":
                content = case["test_patch"]
            elif not state["copied"]:
                content = case["fix_patch"]
            else:
                content = "still reference" if failure == "copy_mismatch" else patch
            return hashlib.sha256(content.encode()).hexdigest() + "  patch"
        assert cmd[cmd.index("-w") + 1] == f"/home/{name}"
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
            assert (
                output / "work" / repo / "evals" / f"pr-{number}" / "fix-patch-run.log"
            ).is_file()
            (output / "grade").mkdir()
            (output / "grade/final_report.json").write_text(
                json.dumps(
                    {
                        "total_instances": 1,
                        "resolved_instances": 1,
                        "completed_instances": 1,
                        "error_instances": 0,
                        "submitted_ids": [
                            (
                                "other/repo:pr-1"
                                if failure == "wrong_report_identity"
                                else f"{repo}:pr-{number}"
                            )
                        ],
                    }
                )
            )
        # Gson retains an unrelated failing class; ponyc lacks stdlib linkage
        # dependencies. The native grader uses required test labels in both.
        return SimpleNamespace(
            returncode=native_exit_code if cmd[:2] == ["docker", "exec"] else 0
        )

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
    if failure is None:
        assert result["native_exit_code"] == native_exit_code
    if failure in {"startup_timeout", "copy_mismatch", "wrong_tree", "wrong_script"}:
        assert not state["executed"]
