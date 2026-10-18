#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Grade one original Pro v1 task with pinned official scripts, without models."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.contextbench_native_repos import PRO_REPOS

HARNESS_REVISION = "66f92766bba642462d4bbe5479e83f91f9211862"
SOURCE_MANIFEST_SHA256 = (
    "b3753f8bebe63604f1527fb610265793c66691058d2d2a38fde664347014364e"
)
SOURCE_MANIFEST_PATH = Path(__file__).with_name("contextbench_pro_sources.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def selected_native_case(path, solver, config):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    selected = [r for r in rows if r.get("instance_id") == solver["original_inst_id"]]
    if len(selected) != 1:
        raise ValueError("Native Pro task is missing or ambiguous")
    row = selected[0]
    if (
        config.get("source") != "Pro"
        or row.get("repo") not in PRO_REPOS
        or row.get("repo") != config["repo"]
        or row.get("base_commit") != config["source_commit"]
    ):
        raise ValueError("Native Pro task differs from the solver source snapshot")
    required = []
    for field in ["fail_to_pass", "pass_to_pass"]:
        names = ast.literal_eval(row[field])
        if not isinstance(names, list) or any(
            not isinstance(name, str) for name in names
        ):
            raise ValueError("Native Pro required test identities are invalid")
        required.extend(names)
    if not required:
        raise ValueError("Native Pro task has no required tests")
    return row, set(required)


def native_environment(upstream, instance_id):
    if upstream is None:
        raise ValueError("Native Pro grading requires --native-upstream")
    manifest_path = SOURCE_MANIFEST_PATH
    if digest(manifest_path) != SOURCE_MANIFEST_SHA256:
        raise ValueError("Native Pro source manifest changed")
    manifest = read_json(manifest_path)
    if manifest["revision"] != HARNESS_REVISION:
        raise ValueError("Native Pro harness revision changed")
    root = upstream.resolve()
    required = [
        "swe_bench_pro_eval.py",
        "helper_code/image_uri.py",
        f"run_scripts/{instance_id}/run_script.sh",
        f"run_scripts/{instance_id}/parser.py",
        f"dockerfiles/base_dockerfile/{instance_id}/Dockerfile",
        f"dockerfiles/instance_dockerfile/{instance_id}/Dockerfile",
    ]
    hashes = {}
    for name in required:
        path = root / name
        if (
            name not in manifest["files"]
            or not path.is_file()
            or (
                not path.resolve().is_relative_to(root)
                or digest(path) != manifest["files"][name]["sha256"]
            )
        ):
            raise ValueError("Native Pro sources differ from their pinned snapshot")
        hashes[name] = digest(path)

    def load_file(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # Load exactly the pinned helper; do not execute an unrelated package init or
    # add the snapshot to sys.path, where it could shadow third-party packages.
    names = ["helper_code", "helper_code.image_uri"]
    previous = {name: sys.modules.get(name) for name in names}
    try:
        package = types.ModuleType("helper_code")
        package.__path__ = []
        sys.modules["helper_code"] = package
        sys.modules["helper_code.image_uri"] = load_file(
            "helper_code.image_uri", root / "helper_code/image_uri.py"
        )
        module = load_file("codenib_native_pro_" + uuid.uuid4().hex, root / required[0])
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
    return module, hashes


def owned_client(client, image_id, container_name, workspace_root, timeout, receipt):
    """Confine the official runner to its local image, workspace and container."""

    def pull(image, **kwargs):
        if image != image_id:
            raise ValueError("Native Pro tried to pull a different image")
        return client.images.get(image_id)

    def run(image, **kwargs):
        if image != image_id:
            raise ValueError("Native Pro image differs from its pinned task image")
        volumes = kwargs.get("volumes", {})
        if len(volumes) != 1 or any(
            not Path(path).resolve().is_relative_to(workspace_root.resolve())
            or mount != {"bind": "/workspace", "mode": "rw"}
            for path, mount in volumes.items()
        ):
            raise ValueError("Native Pro workspace mount differs from its owner")
        kwargs.update(
            name=container_name,
            remove=False,
            network_mode="none",
            mem_limit="4g",
            nano_cpus=2_000_000_000,
            pids_limit=256,
            labels={"codenib.contextbench.owner": container_name},
        )
        container = client.containers.run(image_id, **kwargs)

        def wait():
            result = container.wait(timeout=timeout)
            receipt.update(native_wait_complete=True, native_exit_status=result)
            return result

        return SimpleNamespace(wait=wait)

    return SimpleNamespace(
        images=SimpleNamespace(pull=pull, get=client.images.get),
        containers=SimpleNamespace(run=run),
    )


def grade_pro(args):
    """Own one native Pro invocation; remove only its recorded container.

    The official runner retains patch preparation, command generation and test
    parsing. Its published F2P/P2P subset criterion is applied unchanged. Only local
    image selection, resource/wait bounds and owned-container recovery are wrapped.
    The fresh output directory is the execution intent; removal is completion.
    """
    if not 1 <= args.timeout <= 3600:
        raise ValueError("Native Pro timeout must be between 1 and 3600 seconds")
    args.output = args.output.resolve()
    args.solver_run = args.solver_run.resolve()
    args.dataset = args.dataset.resolve()
    if args.native_upstream is not None:
        args.native_upstream = args.native_upstream.resolve()
    config = read_json(args.solver_run / "config.json")
    solver = read_json(args.solver_run / "solver.json")
    predictions = read_json(args.solver_run / "predictions.json")
    if len(predictions) != 1 or (
        predictions[0]["instance_id"] != solver["original_inst_id"]
        or predictions[0]["model_patch"] != solver["model_patch"]
    ):
        raise ValueError("Native Pro prediction differs from the solver patch")
    case, required = selected_native_case(args.dataset, solver, config)
    for name, expected in config["implementation"].items():
        if digest(args.solver_run / "implementation" / name) != expected:
            raise ValueError("Archived solver implementation changed")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.native_image):
        raise ValueError("Native Pro needs a separate pinned local image ID")
    if args.native_image == config["image_id"]:
        raise ValueError("Native Pro solver and grading images must be separate")
    native, hashes = native_environment(
        args.native_upstream, solver["original_inst_id"]
    )
    import docker

    client = docker.from_env(timeout=120)
    client.images.get(args.native_image)
    args.output.mkdir(parents=True, exist_ok=False)
    name = "codenib-contextbench-pro-" + uuid.uuid4().hex[:16]
    probe_name = name + "-source"
    row = {
        "instance_id": solver["instance_id"],
        "status": "starting",
        "resolved": False,
        "container": name,
        "source_probe_container": probe_name,
        "model_calls": 0,
    }
    write_json(
        args.output / "config.json",
        {
            "solver_sha256": digest(args.solver_run / "solver.json"),
            "prediction_sha256": digest(args.solver_run / "predictions.json"),
            "native_dataset_sha256": digest(args.dataset),
            "native_harness_revision": HARNESS_REVISION,
            "native_harness_hashes": hashes,
            "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
            "image_id": args.native_image,
            "container": name,
            "source_probe_container": probe_name,
            "timeout": args.timeout,
            "source_commit": config["source_commit"],
            "source_tree": config["source_tree"],
            "adapter_sha256": digest(Path(__file__)),
            "scope": "Separate native Pro v1 grading only; no model requests.",
        },
    )
    write_json(args.output / "resolution.json", row)
    old_cwd = Path.cwd()
    try:
        probe = client.containers.run(
            args.native_image,
            name=probe_name,
            detach=True,
            entrypoint="sleep",
            command=["120"],
            network_mode="none",
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            pids_limit=64,
            labels={"codenib.contextbench.owner": probe_name},
        )
        source = probe.exec_run(
            ["git", "rev-parse", config["source_commit"] + "^{tree}"],
            workdir="/app",
        )
        if source.exit_code or source.output.decode().strip() != config["source_tree"]:
            raise ValueError("Native Pro base source differs from the solver tree")
        row["native_base_source_verified"] = True
        probe.remove(force=True)

        def pinned_image(instance, username, repo):
            if instance != case["instance_id"] or repo != case["repo"]:
                raise ValueError("Native Pro image lookup changed task identity")
            return args.native_image

        native.get_dockerhub_image_uri = pinned_image
        bounded = owned_client(
            client, args.native_image, name, args.output, args.timeout, row
        )
        native.docker = SimpleNamespace(from_env=lambda: bounded)
        os.chdir(args.native_upstream)
        output = native.eval_with_docker(
            solver["model_patch"],
            case,
            str(args.output.resolve()),
            "jefzda",
            str(args.native_upstream.resolve() / "run_scripts"),
            block_network=True,
            docker_platform="linux/amd64",
        )
        if not isinstance(output, dict) or not isinstance(output.get("tests"), list):
            raise RuntimeError("Native Pro runner produced no parsed test result")
        if not row.get("native_wait_complete"):
            raise RuntimeError("Native Pro test execution did not complete")
        passed = {x["name"] for x in output["tests"] if x["status"] == "PASSED"}
        row.update(
            status="graded",
            resolved=required <= passed,
            missing_required_tests=sorted(required - passed),
        )
        write_json(args.output / "parsed-tests.json", output)
    except Exception as error:
        row.update(
            status="error",
            resolved=False,
            error_class=type(error).__name__,
            error=str(error),
        )
    finally:
        row["container_removed"] = False
        try:
            for owner in [probe_name, name]:
                try:
                    retained = client.containers.get(owner)
                except docker.errors.NotFound:
                    continue
                if retained.labels.get("codenib.contextbench.owner") != owner:
                    raise ValueError("Native Pro cleanup owner differs")
                retained.remove(force=True)
            for owner in [probe_name, name]:
                try:
                    client.containers.get(owner)
                except docker.errors.NotFound:
                    continue
                raise RuntimeError("Owned native Pro container remains")
            row["container_removed"] = True
        except Exception as error:
            row.update(status="error", resolved=False, cleanup_error=str(error))
        write_json(args.output / "resolution.json", row)
        os.chdir(old_cwd)
    print(json.dumps({k: row[k] for k in ["instance_id", "status", "resolved"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver-run", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--native-image", required=True)
    parser.add_argument("--native-upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    grade_pro(parser.parse_args())


if __name__ == "__main__":
    main()
