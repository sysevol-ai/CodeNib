"""Native Verified task binding, isolated entrypoint and owned cleanup."""

import dataclasses
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import contextbench_verified as verified


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
def test_verified_native_identity_requires_repo_commit_and_unique_task(
    tmp_path, suffix
):
    row = {"instance_id": "native", "base_commit": "base", "repo": "org/repo"}
    solver = {"original_inst_id": "native"}
    config = {"source": "Verified", "source_commit": "base", "repo": "org/repo"}
    path = tmp_path / ("data" + suffix)

    def write(rows):
        if suffix == ".jsonl":
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        else:
            path.write_text(json.dumps(rows))

    write([row])
    assert verified.selected_native_case(path, solver, config) == row
    for key, value in [
        ("source", "Pro"),
        ("source_commit", "other"),
        ("repo", "other/repo"),
    ]:
        with pytest.raises(ValueError, match="source snapshot"):
            verified.selected_native_case(path, solver, {**config, key: value})
    for rows in [[], [row, row]]:
        write(rows)
        with pytest.raises(ValueError, match="missing or ambiguous"):
            verified.selected_native_case(path, solver, config)


def test_native_only_help_requires_no_site_packages():
    script = Path(verified.__file__).resolve()
    result = subprocess.run(
        [sys.executable, "-S", str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--solver-run" in result.stdout


def test_unified_grade_delegates_verified_and_joins_search_usage(monkeypatch, tmp_path):
    from scripts import benchmark_context_solver as solver

    run = tmp_path / "solver"
    run.mkdir()
    (run / "config.json").write_text(
        json.dumps(
            {
                "source": "Verified",
                "retrieval_kind": "search_pair_navigation",
            }
        )
    )
    output = tmp_path / "native"
    output.mkdir()
    args = SimpleNamespace(solver_run=run, output=output)
    calls = []
    monkeypatch.setattr(verified, "grade_verified", lambda actual: calls.append(actual))
    monkeypatch.setattr(solver, "joined_native_result", lambda *paths: {"joined": True})
    solver.grade(args)
    assert calls == [args]
    assert json.loads((output / "whole-run-result.json").read_text()) == {
        "joined": True
    }


def test_native_client_only_creates_its_owner_with_bounded_resources():
    calls = []
    client = SimpleNamespace(
        containers=SimpleNamespace(create=lambda **kw: calls.append(kw))
    )
    verified.owned_native_client(client, "owned")
    with pytest.raises(ValueError, match="owner"):
        client.containers.create(name="other")
    assert not calls
    client.containers.create(name="owned", entrypoint=["sleep"], network_disabled=False)
    assert calls == [
        {
            "name": "owned",
            "entrypoint": [],
            "network_disabled": True,
            "mem_limit": "4g",
            "nano_cpus": 2_000_000_000,
            "pids_limit": 256,
        }
    ]


@pytest.mark.parametrize("outcome", [True, False, "incomplete", "exception", "cleanup"])
def test_native_grader_records_real_status_and_exact_owner_cleanup(
    monkeypatch, tmp_path, outcome
):
    run = tmp_path / "solver"
    run.mkdir()
    image_id = "sha256:" + "1" * 64
    config = {
        "source": "Verified",
        "source_commit": "base",
        "repo": "org/repo",
        "image_id": image_id,
        "implementation": {},
    }
    solver = {
        "instance_id": "canonical",
        "original_inst_id": "native",
        "model_patch": "candidate patch",
    }
    prediction = {
        "instance_id": "native",
        "model_patch": "candidate patch",
        "model_name_or_path": "fixed-model",
    }
    for name, value in [
        ("config", config),
        ("solver", solver),
        ("predictions", [prediction]),
    ]:
        (run / (name + ".json")).write_text(json.dumps(value))
    dataset = tmp_path / "native.json"
    dataset.write_text(
        json.dumps(
            [{"instance_id": "native", "base_commit": "base", "repo": "org/repo"}]
        )
    )

    class NotFound(Exception):
        pass

    live = {}
    created = []

    def create(**kwargs):
        name = kwargs["name"]

        def remove(force):
            assert force
            if outcome == "cleanup":
                raise RuntimeError("fixture cleanup failure")
            live.pop(name)

        live[name] = SimpleNamespace(remove=remove)
        created.append(kwargs)
        return live[name]

    def get(name):
        if name not in live:
            raise NotFound(name)
        return live[name]

    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda name: image_id if name == image_id else None),
        containers=SimpleNamespace(create=create, get=get),
    )

    @dataclasses.dataclass
    class Spec:
        instance_id: str

        @property
        def eval_script(self):
            return "echo native fixture\n"

        def get_instance_container_name(self, run_id):
            return "native." + run_id

    def run_instance(spec, pred, rm, rebuild, actual_client, run_id, timeout):
        assert pred == prediction and not rm and not rebuild and timeout == 900
        assert spec.instance_image_key == image_id
        actual_client.containers.create(name=spec.get_instance_container_name(run_id))
        if outcome == "exception":
            raise RuntimeError("fixture native failure")
        if outcome == "incomplete":
            return {"completed": False}
        report = (
            native.RUN_EVALUATION_LOG_DIR / run_id / "fixed-model/native/report.json"
        )
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"native": {"resolved": outcome is not False}}))
        return {"completed": True}

    native = SimpleNamespace(run_instance=run_instance)

    @contextmanager
    def capture(module, directory):
        assert module is native
        yield

    monkeypatch.setattr(verified, "native_environment", lambda: {"fixture": "pinned"})
    monkeypatch.setattr(verified, "captured_verified_output", capture)
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(
            from_env=lambda **kwargs: client, errors=SimpleNamespace(NotFound=NotFound)
        ),
    )
    monkeypatch.setitem(
        sys.modules, "swebench.harness", SimpleNamespace(run_evaluation=native)
    )
    monkeypatch.setitem(
        sys.modules,
        "swebench.harness.test_spec.test_spec",
        SimpleNamespace(
            TestSpec=Spec,
            make_test_spec=lambda row, namespace: Spec(row["instance_id"]),
        ),
    )
    args = SimpleNamespace(
        solver_run=run, dataset=dataset, output=tmp_path / "native", timeout=900
    )
    if type(outcome) is bool:
        verified.grade_verified(args)
    else:
        with pytest.raises(RuntimeError, match="incomplete"):
            verified.grade_verified(args)
    record = json.loads((args.output / "resolution.json").read_text())
    assert record["instance_id"] == "canonical"
    assert record["status"] == ("graded" if type(outcome) is bool else "error")
    assert record["resolved"] is (outcome is True)
    assert record["owned_container_removed"] is (outcome != "cleanup")
    assert created[0]["name"] == record["container"]
    assert created[0]["entrypoint"] == [] and created[0]["network_disabled"]
    assert bool(live) is (outcome == "cleanup")
