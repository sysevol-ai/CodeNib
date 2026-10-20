#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Grade one Verified solver patch in an isolated native environment, no models."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.contextbench_verified_output import (
    EXECUTOR_SHA256,
    captured_verified_output,
)

HARNESS_HASHES = {
    "harness/run_evaluation.py": (
        "6959f0b4e4eaf979771f529b88e3e9df1daa7fe86bc4291feec2e7d320bf7f2e"
    ),
    "harness/docker_utils.py": EXECUTOR_SHA256,
    "harness/docker_build.py": (
        "5278842b60a7d38256f95f93c915dc84de2b8b4f286e9baae1b19280f768e484"
    ),
    "harness/grading.py": "88fc500ebaf692a53457149cec24dcf82dfa2d48f98021a894a087d275f61df4",
    "harness/log_parsers/python.py": (
        "42f564edfee3c21751739bbf09d60cf3a3ecdc58ac5cf45717dc6b47a85d7459"
    ),
    "harness/test_spec/test_spec.py": (
        "f9ab368091b5770d2ab6e842e7d56161f87e70a20519576731c122e310820f4a"
    ),
    "harness/test_spec/python.py": (
        "bb66f607d1932db995a11fd6d6dc2f1c86a695257385bc88f98409582c0c469a"
    ),
    "harness/constants/__init__.py": (
        "c12fe2671fd8b7d8af8f5c711fceb2ca684254e2c1b4cde448422a44b8d04e35"
    ),
    "harness/constants/python.py": (
        "ce4f0e0f0939745944d84c51356530f5da57f486ac839523556d7ffdb2f9072b"
    ),
    "harness/utils.py": "cdc6bf415354f504c3cac2d82200e3d8a3d5cc0fe87b272e28863b66804c5068",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def read_json(path):
    return json.loads(path.read_text())


def selected_native_case(path, solver, config):
    if path.suffix == ".jsonl":
        rows = [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]
    else:
        rows = json.loads(path.read_text())
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Native Verified dataset must contain task records")
    selected = [r for r in rows if r.get("instance_id") == solver["original_inst_id"]]
    if len(selected) != 1:
        raise ValueError("Native Verified task is missing or ambiguous")
    row = selected[0]
    if (
        config.get("source", "Verified") != "Verified"
        or row.get("base_commit") != config["source_commit"]
        or row.get("repo") != config["repo"]
    ):
        raise ValueError("Native Verified task differs from the solver source snapshot")
    return row


def native_environment():
    if importlib.metadata.version("swebench") != "4.1.0":
        raise ValueError("Native grading requires isolated swebench==4.1.0")
    import swebench

    package = Path(swebench.__file__).parent
    hashes = {name: digest(package / name) for name in HARNESS_HASHES}
    if hashes != HARNESS_HASHES:
        raise ValueError("Native Verified modules differ from the pinned harness")
    return hashes


def owned_native_client(client, container_name):
    """Only this invocation's native container may be created through the client."""
    containers = client.containers
    create = containers.create

    def create_owned(*args, **kwargs):
        if kwargs.get("name") != container_name:
            raise ValueError("Unexpected native Verified container owner")
        kwargs.update(
            entrypoint=[],
            network_disabled=True,
            mem_limit="4g",
            nano_cpus=2_000_000_000,
            pids_limit=256,
        )
        return create(*args, **kwargs)

    containers.create = create_owned
    return client


def grade_verified(args):
    """Own one native run; exact container removal is its completion boundary.

    The native harness owns execution and its normal cleanup. This invocation owns
    recovery of only the recorded unique container if that cleanup fails. It never
    pulls an unpinned image, passes credentials, or supplies native data to a model.
    """
    if not 1 <= args.timeout <= 3600:
        raise ValueError("Native Verified timeout must be between 1 and 3600 seconds")
    config = read_json(args.solver_run / "config.json")
    solver = read_json(args.solver_run / "solver.json")
    predictions = read_json(args.solver_run / "predictions.json")
    if (
        len(predictions) != 1
        or predictions[0]["instance_id"] != solver["original_inst_id"]
        or predictions[0]["model_patch"] != solver["model_patch"]
    ):
        raise ValueError("Native Verified prediction differs from the solver patch")
    selected = selected_native_case(args.dataset, solver, config)
    for name, expected in config["implementation"].items():
        if digest(args.solver_run / "implementation" / name) != expected:
            raise ValueError("Archived solver implementation changed")
    hashes = native_environment()
    import docker
    from swebench.harness import run_evaluation
    from swebench.harness.test_spec.test_spec import TestSpec, make_test_spec

    image_id = config["image_id"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("Native Verified grading requires a pinned local image ID")
    client = docker.from_env(timeout=120)
    client.images.get(image_id)

    class PinnedImageSpec(TestSpec):
        @property
        def instance_image_key(self):
            return image_id

    spec = PinnedImageSpec(
        **dataclasses.asdict(make_test_spec(selected, namespace="swebench"))
    )
    args.output.mkdir(parents=True, exist_ok=False)
    run_id = "codenib-" + uuid.uuid4().hex[:12]
    container_name = spec.get_instance_container_name(run_id)
    client = owned_native_client(client, container_name)
    write_json(
        args.output / "config.json",
        {
            "solver_run": str(args.solver_run.resolve()),
            "solver_sha256": digest(args.solver_run / "solver.json"),
            "prediction_sha256": digest(args.solver_run / "predictions.json"),
            "dataset_sha256": digest(args.dataset),
            "native_harness_version": "4.1.0",
            "native_harness_hashes": hashes,
            "image_id": image_id,
            "run_id": run_id,
            "container": container_name,
            "container_limits": {
                "network": "none",
                "memory": "4g",
                "cpus": 2,
                "pids": 256,
            },
            "timeout": args.timeout,
            "adapter_sha256": digest(Path(__file__)),
            "output_transport_sha256": digest(
                ROOT / "scripts/contextbench_verified_output.py"
            ),
            "scope": "Native Verified patch/test application and original grading only",
        },
    )
    (args.output / "eval.sh").write_text(spec.eval_script)
    run_evaluation.RUN_EVALUATION_LOG_DIR = args.output / "logs"
    record = {
        "instance_id": solver["instance_id"],
        "status": "error",
        "resolved": False,
        "container": container_name,
        "model_calls": 0,
    }
    try:
        with captured_verified_output(run_evaluation, args.output / "stdout"):
            result = run_evaluation.run_instance(
                spec, predictions[0], False, False, client, run_id, timeout=args.timeout
            )
        if result and result.get("completed"):
            report_path = (
                args.output
                / "logs"
                / run_id
                / predictions[0]["model_name_or_path"].replace("/", "__")
                / solver["original_inst_id"]
                / "report.json"
            )
            report = read_json(report_path)
            if type(report[solver["original_inst_id"]].get("resolved")) is not bool:
                raise ValueError("Native Verified report has no boolean resolution")
            record.update(
                status="graded",
                resolved=report[solver["original_inst_id"]]["resolved"],
                native_report=report,
            )
    except Exception as error:
        record.update(error_class=type(error).__name__, error=str(error))
    finally:
        try:
            client.containers.get(container_name).remove(force=True)
        except docker.errors.NotFound:
            pass
        except Exception as error:
            record.update(
                status="error", resolved=False, cleanup_error_class=type(error).__name__
            )
        try:
            client.containers.get(container_name)
        except docker.errors.NotFound:
            record["owned_container_removed"] = True
        except Exception as error:
            record.update(
                status="error",
                resolved=False,
                owned_container_removed=False,
                cleanup_check_error_class=type(error).__name__,
            )
        else:
            record.update(status="error", resolved=False, owned_container_removed=False)
        write_json(args.output / "resolution.json", record)
    if record["status"] != "graded":
        raise RuntimeError(
            "Native Verified grading incomplete; inspect resolution.json"
        )
    print(json.dumps({k: record[k] for k in ["instance_id", "status", "resolved"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver-run", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    grade_verified(parser.parse_args())


if __name__ == "__main__":
    main()
