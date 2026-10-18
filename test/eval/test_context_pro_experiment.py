"""Pro v1 native identity, pinned source loading and bounded owner execution."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import contextbench_pro as pro


def test_pro_native_identity_and_required_tests(tmp_path):
    row = {
        "instance_id": "native",
        "repo": "NodeBB/NodeBB",
        "base_commit": "base",
        "fail_to_pass": "['fix']",
        "pass_to_pass": "['stable']",
    }
    path = tmp_path / "data.jsonl"
    path.write_text(json.dumps(row) + "\n")
    solver = {"original_inst_id": "native"}
    config = {"source": "Pro", "repo": "NodeBB/NodeBB", "source_commit": "base"}
    assert pro.selected_native_case(path, solver, config) == (row, {"fix", "stable"})
    for key, value in [
        ("source", "Verified"),
        ("repo", "other/repo"),
        ("source_commit", "wrong"),
    ]:
        with pytest.raises(ValueError, match="source snapshot"):
            pro.selected_native_case(path, solver, {**config, key: value})
    path.write_text((json.dumps(row) + "\n") * 2)
    with pytest.raises(ValueError, match="ambiguous"):
        pro.selected_native_case(path, solver, config)


def test_pro_source_loader_restores_helpers_and_never_executes_package_init(
    tmp_path, monkeypatch
):
    instance = "native"
    root = tmp_path / "snapshot"
    files = {
        "swe_bench_pro_eval.py": (
            "from helper_code.image_uri import marker\nloaded_marker=marker\n"
        ),
        "helper_code/image_uri.py": "marker='pinned helper'\n",
        "run_scripts/native/run_script.sh": "echo tests\n",
        "run_scripts/native/parser.py": "pass\n",
        "dockerfiles/base_dockerfile/native/Dockerfile": "FROM base\n",
        "dockerfiles/instance_dockerfile/native/Dockerfile": "FROM instance\n",
    }
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "helper_code/__init__.py").write_text(
        "raise RuntimeError('untrusted init')\n"
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "revision": pro.HARNESS_REVISION,
                "files": {name: {"sha256": pro.digest(root / name)} for name in files},
            }
        )
    )
    monkeypatch.setattr(pro, "SOURCE_MANIFEST_PATH", manifest)
    monkeypatch.setattr(pro, "SOURCE_MANIFEST_SHA256", pro.digest(manifest))
    previous = SimpleNamespace(marker="existing helper package")
    monkeypatch.setitem(sys.modules, "helper_code", previous)
    module, hashes = pro.native_environment(root, instance)
    assert module.loaded_marker == "pinned helper" and len(hashes) == 6
    assert sys.modules["helper_code"] is previous
    (root / "run_scripts/native/parser.py").write_text("changed\n")
    with pytest.raises(ValueError, match="pinned snapshot"):
        pro.native_environment(root, instance)


def test_pro_owned_client_preserves_native_command_and_bounds_wait(tmp_path):
    calls = []
    waits = []
    image = "sha256:" + "a" * 64
    target = SimpleNamespace(wait=lambda **kw: waits.append(kw) or {"StatusCode": 0})
    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda name: name),
        containers=SimpleNamespace(
            run=lambda *a, **kw: calls.append((a, kw)) or target
        ),
    )
    receipt = {}
    bounded = pro.owned_client(client, image, "owned", tmp_path, 60, receipt)
    assert bounded.images.pull(image) == image
    with pytest.raises(ValueError, match="different image"):
        bounded.images.pull("mutable:tag")
    command = ["-c", "bash /workspace/entryscript.sh"]
    container = bounded.containers.run(
        image,
        volumes={str(tmp_path / "work"): {"bind": "/workspace", "mode": "rw"}},
        command=command,
        entrypoint="/bin/bash",
        remove=True,
    )
    assert container.wait() == {"StatusCode": 0}
    assert waits == [{"timeout": 60}]
    assert calls[0][1]["command"] == command
    assert calls[0][1]["name"] == "owned" and not calls[0][1]["remove"]
    assert calls[0][1]["network_mode"] == "none" and receipt["native_wait_complete"]
    with pytest.raises(ValueError, match="workspace mount"):
        bounded.containers.run(
            image, volumes={"/outside": {"bind": "/workspace", "mode": "rw"}}
        )


def test_pro_cpu_profile_uses_allowed_affinity_and_bounds_auto_workers(monkeypatch):
    monkeypatch.setattr(pro.os, "sched_getaffinity", lambda pid: {7, 3, 5})
    assert pro.cpu_resources(None) == {}
    assert pro.cpu_resources(2) == {
        "cpuset_cpus": "3,5",
        "environment": {"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"},
    }
    for count in [0, 3, True]:
        with pytest.raises(ValueError, match="one or two"):
            pro.cpu_resources(count)
    monkeypatch.setattr(pro.os, "sched_getaffinity", lambda pid: {3})
    with pytest.raises(ValueError, match="fewer allowed CPUs"):
        pro.cpu_resources(2)


def test_pro_cpu_profile_preserves_command_env_and_native_limits(tmp_path):
    calls = []
    target = SimpleNamespace(wait=lambda **kwargs: {"StatusCode": 0})
    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda name: name),
        containers=SimpleNamespace(
            run=lambda *args, **kwargs: calls.append(kwargs) or target
        ),
    )
    resources = {
        "cpuset_cpus": "3,5",
        "environment": {"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"},
    }
    receipt = {}
    bounded = pro.owned_client(
        client, "image", "owned", tmp_path, 60, receipt, resources=resources
    )
    command = ["-c", "bash /workspace/entryscript.sh"]
    bounded.containers.run(
        "image",
        volumes={str(tmp_path / "work"): {"bind": "/workspace", "mode": "rw"}},
        command=command,
        environment={"LANG": "C.UTF-8"},
    )
    assert calls[0]["command"] == command
    assert calls[0]["cpuset_cpus"] == "3,5"
    assert calls[0]["environment"] == {
        "LANG": "C.UTF-8",
        "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
    }
    assert calls[0]["pids_limit"] == 256
    assert calls[0]["nano_cpus"] == 2_000_000_000
    assert calls[0]["network_mode"] == "none"
    assert receipt["native_cpu_resources"] == resources


def test_pro_jest_profile_preserves_pinned_script_and_parser(tmp_path):
    original = "npx jest --verbose --silent ${test_files[@]} 2>&1\n"
    native = SimpleNamespace(
        load_local_script=lambda directory, identity, name: (
            original if name == "run_script.sh" else "pinned parser"
        )
    )
    profile = pro.limit_jest_workers(native, "case", tmp_path, 2, tmp_path)
    assert (tmp_path / "original-run-script.sh").read_text() == original
    effective = native.load_local_script(str(tmp_path), "case", "run_script.sh")
    assert effective.replace("--maxWorkers=2 ", "") == original
    assert (tmp_path / "resource-run-script.sh").read_text() == effective
    assert profile["original_run_script_sha256"] == pro.digest(
        tmp_path / "original-run-script.sh"
    )
    assert profile["effective_run_script_sha256"] == pro.digest(
        tmp_path / "resource-run-script.sh"
    )
    assert (
        native.load_local_script(str(tmp_path), "case", "parser.py") == "pinned parser"
    )
    with pytest.raises(ValueError, match="task identity"):
        native.load_local_script(str(tmp_path), "different-case", "run_script.sh")
    with pytest.raises(ValueError, match="task identity"):
        native.load_local_script(str(tmp_path / "other"), "case", "run_script.sh")


def test_pro_jest_profile_rejects_changed_concurrency_command(tmp_path):
    for script in ["npx other-runner", "npx jest --maxWorkers=10"]:
        native = SimpleNamespace(load_local_script=lambda *args, value=script: value)
        with pytest.raises(ValueError, match="resource profile"):
            pro.limit_jest_workers(native, "case", tmp_path, 2, tmp_path)


@pytest.mark.parametrize("fault", [None, "missing_test", "wait", "tree", "cleanup"])
def test_pro_grader_preserves_outcomes_and_removes_exact_owners(
    tmp_path, monkeypatch, fault
):
    run = tmp_path / "solver"
    run.mkdir()
    image = "sha256:" + "a" * 64
    solver = {
        "instance_id": "canonical",
        "original_inst_id": "native",
        "model_patch": "candidate",
    }
    config = {
        "source": "Pro",
        "repo": "NodeBB/NodeBB",
        "source_commit": "base",
        "source_tree": "tree",
        "image_id": "sha256:" + "b" * 64,
        "implementation": {},
    }
    for name, value in [
        ("solver", solver),
        ("config", config),
        ("predictions", [{"instance_id": "native", "model_patch": "candidate"}]),
    ]:
        (run / (name + ".json")).write_text(json.dumps(value))
    data = tmp_path / "data.jsonl"
    data.write_text(
        json.dumps(
            {
                "instance_id": "native",
                "repo": "NodeBB/NodeBB",
                "base_commit": "base",
                "fail_to_pass": "['fix']",
                "pass_to_pass": "['stable']",
            }
        )
        + "\n"
    )
    args = SimpleNamespace(
        solver_run=run,
        dataset=data,
        output=tmp_path / "out",
        native_image=image,
        native_upstream=tmp_path,
        timeout=60,
    )

    class NotFound(Exception):
        pass

    live, created = {}, []

    def start(image, **kwargs):
        name = kwargs["name"]
        labels = kwargs["labels"]
        created.append(name)

        def remove(force):
            assert force
            if fault == "cleanup" and not name.endswith("-source"):
                raise RuntimeError("cleanup failed")
            live.pop(name, None)

        def wait(**kwargs):
            assert kwargs["timeout"] == 60
            if fault == "wait":
                raise TimeoutError("native wait")
            return {"StatusCode": 0}

        live[name] = SimpleNamespace(
            labels=labels,
            remove=remove,
            wait=wait,
            exec_run=lambda *a, **kw: SimpleNamespace(
                exit_code=0, output=b"wrong\n" if fault == "tree" else b"tree\n"
            ),
        )
        return live[name]

    def get(name):
        if name not in live:
            raise NotFound(name)
        return live[name]

    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda name: name),
        containers=SimpleNamespace(run=start, get=get),
    )
    native = SimpleNamespace()

    def evaluate(patch, case, output, username, scripts, **kwargs):
        assert patch == "candidate" and kwargs["block_network"]
        actual_image = native.get_dockerhub_image_uri(
            case["instance_id"], username, case["repo"]
        )
        bounded = native.docker.from_env()
        bounded.images.pull(actual_image)
        container = bounded.containers.run(
            actual_image,
            volumes={
                str(Path(output) / "workspace"): {"bind": "/workspace", "mode": "rw"}
            },
        )
        container.wait()
        return {
            "tests": [{"name": "fix", "status": "PASSED"}]
            + (
                []
                if fault == "missing_test"
                else [{"name": "stable", "status": "PASSED"}]
            )
        }

    native.eval_with_docker = evaluate
    monkeypatch.setattr(
        pro, "native_environment", lambda *a: (native, {"fixture": "pin"})
    )
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(
            from_env=lambda **kw: client, errors=SimpleNamespace(NotFound=NotFound)
        ),
    )
    old_cwd = Path.cwd()
    pro.grade_pro(args)
    assert Path.cwd() == old_cwd
    record = json.loads((args.output / "resolution.json").read_text())
    assert record["resolved"] is (fault is None)
    assert record["status"] == (
        "graded" if fault in {None, "missing_test"} else "error"
    )
    assert record["container_removed"] is (fault != "cleanup")
    assert all(
        name in {record["container"], record["source_probe_container"]}
        for name in created
    )
    assert bool(live) is (fault == "cleanup")
