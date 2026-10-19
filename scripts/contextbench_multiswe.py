#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Native Multi grading for the frozen canonical500 repository inventory.

Reference patches/tests enter only this grader. All31 registered repo classes
are pinned; each dispatched case still needs source/native environment checks.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.contextbench_native_repos import MULTI_REPOS, POLY_REPOS, PRO_REPOS

HARNESS_REVISION = "24f493f8a103e72312ded4f6b9c89f081d69cb09"
HARNESS_HASHES = {
    "harness/gen_report.py": "02ebc8a5414898d12f4f5a9ba0c11a8f57c9f34a0bdc02c2311afac9f654847d",
    "harness/report.py": "5a025fd496d42c4b7377fc0702d64c6d0e356b117eaf2face47e73a52c29902f",
    "harness/test_result.py": (
        "5411af794920cf4b170fe9dbe8c21c12cc63e2bbe2280d6d82acb850f4808be3"
    ),
    "harness/dataset.py": "dd49f55baf63b60fff309b6a5b2a1826697e2b85ad1a9bccff18321dcdc200fc",
    "harness/image.py": "86074812495b97026efb42c57acbf7738864b1f0167f99e3b9f9309458972ae9",
    "harness/instance.py": "93da4f6db69e006949bba856e22e4a464b5e93140590258eda38833ec5f19a9e",
    "harness/pull_request.py": (
        "32b49f48b39124f67727f408898bd96cce91c0a362faa716ac858dcb0b0b47c7"
    ),
}
HARNESS_HASHES.update(
    {
        "harness/repos/c/facebook/zstd.py": (
            "ccb953474042a9f9610a03f0ddb8669b00b4ad39ee6c3f0108ae15fc02ed4ee1"
        ),
        "harness/repos/c/jqlang/jq.py": (
            "e523664fcf8a1b728f5d4d77caeebc7cecd34c575f295fdb66a441b910e3a8b0"
        ),
        "harness/repos/c/ponylang/ponyc.py": (
            "be551b5b2b3bb0367e12b1f46a5bc5a5d7e5ab161cced3921cd05e18f90ae744"
        ),
        "harness/repos/cpp/catchorg/catch2.py": (
            "0f66a695e470a56908ee056e5174a27d4617a8d5da059944bea0f16310771b28"
        ),
        "harness/repos/cpp/fmtlib/fmt.py": (
            "d0a5146eb77b4416cccb86216c3df08f200d58c33bb158d953aaac385f922918"
        ),
        "harness/repos/cpp/nlohmann/json.py": (
            "eb029a49f751d532c8241583ff0e7f60b129a1c637e40aa9ecd68c2d6bc8a79d"
        ),
        "harness/repos/cpp/simdjson/simdjson.py": (
            "90875649cc32fa7dc9ef0f930ef97c05855b47fbea60c02fca06e51d653ba31e"
        ),
        "harness/repos/golang/cli/cli.py": (
            "3d8b89caeebffd4a41791c41af14de467f7074d522a45c5ea87db9c94ca7d913"
        ),
        "harness/repos/golang/grpc/grpc_go.py": (
            "b3e83b2ae82ee196d0cfd99a704e714df4b35e5f4f9bbebf5e97e24ae31da58d"
        ),
        "harness/repos/golang/zeromicro/go_zero.py": (
            "ca72e6d89f078d2b7349d11524f93a7e979f12a485e4620cb6d1262b431e84cf"
        ),
        "harness/repos/java/alibaba/fastjson2.py": (
            "87c3bba2bbb6ee10273034030bac4033544676c5e100ee6c60c90920fe80dafa"
        ),
        "harness/repos/java/elastic/logstash.py": (
            "9656082f97093b6b729f7c26b44a385e72a0e86ea2105096b1be22db1ba5d17d"
        ),
        "harness/repos/java/fasterxml/jackson_core.py": (
            "2d261db299901827c865a4a34345f9a0383c3a0303b49829448263c7d525cf4d"
        ),
        "harness/repos/java/fasterxml/jackson_databind.py": (
            "4423f336b0643e267c0da3eb442bd15558803b248bf3474446b9953d146e7a74"
        ),
        "harness/repos/java/fasterxml/jackson_dataformat_xml.py": (
            "f43e309e6e998e9334ac7d7581e26df7f59da10b5441a6bb197f3061a2bbc65b"
        ),
        "harness/repos/java/google/gson.py": (
            "faec1d6ba360ab902a663718b9a8668d1a46a860bc613980b310107153adf890"
        ),
        "harness/repos/java/mockito/mockito.py": (
            "4fcb4ff8250696964f95a60954a5a5262ac89cea3afdfbee2d85b341dad11405"
        ),
        "harness/repos/javascript/axios/axios.py": (
            "6bd53130e1e974ceae1576d91874e23a0d74b46928f0c34770bc0e3121c6e109"
        ),
        "harness/repos/javascript/expressjs/express.py": (
            "a673518e3b4d9e9e2396f97aacdc5c803d7e2298ce07dfd748cbb9f67ce36291"
        ),
        "harness/repos/javascript/iamkun/dayjs.py": (
            "6ea5cc6572d192d54ad731d8fd3f63c36e60a0918c4df08d379cd85bf2265d70"
        ),
        "harness/repos/javascript/sveltejs/svelte.py": (
            "84e7c46181e5cc0008e326a3581bfdee98793146984266f281db48b818b27e6c"
        ),
        "harness/repos/rust/BurntSushi/ripgrep.py": (
            "aca2ad7ec634a275994c9fe9bf0c91a9cc875b62f90b1c754146b63835c7f8a7"
        ),
        "harness/repos/rust/clap_rs/clap.py": (
            "696693c5804f91bebb7424ebe73d5730f5e865536ee087d454ab948dee5e4cf6"
        ),
        "harness/repos/rust/nushell/nushell.py": (
            "648183c795b6507e209e41252a34d5b3fb0e47dbab91e60a64f96df2d03bcd6a"
        ),
        "harness/repos/rust/rayon_rs/rayon.py": (
            "0e8b6781c51aff395d846ebe8dba66ea20aa0036a6156aed8523654cc5b61a99"
        ),
        "harness/repos/rust/tokio_rs/bytes.py": (
            "9565684b4813e6b7c562071498ffed32c653b427ff3eed39987962383fa2ce37"
        ),
        "harness/repos/rust/tokio_rs/tokio.py": (
            "7b4a826d139bea29fceca37a68e0da3654d88d5cbb2be609489680b115778f62"
        ),
        "harness/repos/rust/tokio_rs/tracing.py": (
            "1cf11f91b0e9df44295785fe2cc1d69a799e05ab3524e67dd4244b57676d210d"
        ),
        "harness/repos/typescript/darkreader/darkreader.py": (
            "ae880afbd389f0356dd533558a9f418afccd183f87eaad8cbdca48832dce2e58"
        ),
        "harness/repos/typescript/mui/material_ui.py": (
            "c321c73cf18bec405aed1381d795967176db0fa17bd2d12dd9f2bb834ddb381b"
        ),
        "harness/repos/typescript/vuejs/core.py": (
            "f154469392f1c52a5d8756c8f5332be35347b8b3bf4dd739a443b5ad4a5f3ce5"
        ),
    }
)

REFERENCE_PATHS = (
    "/home/fix.patch",
    "/home/test.patch",
    "/home/fix-run.sh",
    "/home/test-run.sh",
    "/home/prepare.sh",
    "/home/check_git_changes.sh",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def solver_cwd(source, repo):
    if source == "Verified":
        return "/testbed"
    if source == "Multi" and repo in MULTI_REPOS:
        return "/home/" + repo.split("/")[1]
    if source == "Poly" and repo in POLY_REPOS:
        return "/testbed"
    if source == "Pro" and repo in PRO_REPOS:
        return "/app"
    raise ValueError("Unsupported native repository for solver environment")


def validate_source_only_image(environment, commit):
    """Reject known native reference artifacts and retained history before SDK use."""
    command = (
        "git rev-list --all --count && git remote && "
        "git reflog --all --format=%H | sort -u"
    )
    result = environment.execute(command)
    # A reflog may name the initial source commit, never any prior history.
    if result["returncode"] or result["output"].strip().splitlines() not in (
        ["1"],
        ["1", commit],
    ):
        raise ValueError(
            "Solver image must have one source commit without prior history"
        )
    result = environment.execute(
        "for path in "
        + " ".join(REFERENCE_PATHS)
        + '; do if test -e "$path" || test -L "$path"; then exit 1; fi; done'
    )
    if result["returncode"]:
        raise ValueError("Solver image contains native reference artifacts")
    return {"one_source_commit": True, "reference_artifacts_absent": True}


def native_environment():
    if importlib.metadata.version("multi-swe-bench") != "1.1.0":
        raise ValueError(
            "Native Multi grading requires isolated multi-swe-bench==1.1.0"
        )
    import multi_swe_bench

    package = Path(multi_swe_bench.__file__).parent
    hashes = {name: digest(package / name) for name in HARNESS_HASHES}
    if hashes != HARNESS_HASHES:
        raise ValueError("Native Multi grading modules differ from the pinned harness")
    return hashes


def selected_native_case(path, solver, config):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    selected = [
        row for row in rows if row.get("instance_id") == solver["original_inst_id"]
    ]
    if len(selected) != 1:
        raise ValueError("Native Multi prediction identity is missing or ambiguous")
    row = selected[0]
    number = row.get("number")
    repo = f"{row.get('org')}/{row.get('repo')}"
    if (
        config.get("source") != "Multi"
        or config.get("repo") not in MULTI_REPOS
        or config.get("repo") != repo
        or type(number) is not int
        or number <= 0
        or row["instance_id"] != f"{row['org']}__{row['repo']}-{number}"
        or row.get("base", {}).get("sha") != config["source_commit"]
    ):
        raise ValueError("Native Multi task differs from the supported source snapshot")
    return row


def native_script_hashes(case):
    """Authenticate the entrypoints generated by the pinned native harness."""
    from multi_swe_bench.harness.report import ReportTask

    instance = ReportTask(
        org=case["org"],
        repo=case["repo"],
        number=case["number"],
        instance_dir=Path("."),
    ).instance
    return {
        file.name: hashlib.sha256(file.content.encode()).hexdigest()
        for file in instance.dependency().files()
        if file.name in {"run.sh", "test-run.sh", "fix-run.sh"}
    }


def command(args, *, timeout=60, **kwargs):
    return subprocess.run(
        args, text=True, capture_output=True, timeout=timeout, check=True, **kwargs
    ).stdout.strip()


def grade_multi(args):
    """Own one fresh native grader; remove it synchronously on every exit.

    Only this invocation owns the recorded container; it has no host mounts.
    Networking requires an explicit native-only option. The finally block's
    successful removal is the completion boundary.
    A bounded container sleep also ends an orphan after an abrupt owner death.
    The reference production patch is authenticated, then replaced by the exact
    solver patch before the original native test script can execute.
    """
    if not 1 <= args.timeout <= 3600:
        raise ValueError("Native Multi timeout must be between 1 and 3600 seconds")
    memory_gib = getattr(args, "native_memory_gib", 3)
    if memory_gib not in (3, 8):
        raise ValueError("Native Multi memory must be 3 or 8 GiB")
    network = getattr(args, "native_network", "none")
    if network not in ("none", "bridge"):
        raise ValueError("Native Multi network must be none or bridge")
    pids_limit = getattr(args, "native_pids_limit", 256)
    if pids_limit not in (256, 1024):
        raise ValueError("Native Multi process limit must be 256 or 1024")
    test_workers = getattr(args, "native_test_workers", None)
    if test_workers not in (None, 2):
        raise ValueError("Native Ponyc test workers must be 2")
    cpu_count = getattr(args, "native_cpu_count", None)
    if cpu_count not in (None, 2):
        raise ValueError("Native Multi CPU count must be 2")
    cpu_affinity = None
    if cpu_count is not None:
        try:
            allowed_cpus = sorted(os.sched_getaffinity(0))
        except (AttributeError, OSError) as exc:
            raise ValueError("Native Multi CPU affinity is unavailable") from exc
        if len(allowed_cpus) < cpu_count:
            raise ValueError("Native Multi needs at least two allowed CPUs")
        cpu_affinity = ",".join(map(str, allowed_cpus[:cpu_count]))
    image = getattr(args, "native_image", None)
    if not image or not re.fullmatch(r"(?:sha256:|.+@sha256:)[a-f0-9]{64}", image):
        raise ValueError("Multi grading needs a separate pinned --native-image")
    hashes = native_environment()
    config = json.loads((args.solver_run / "config.json").read_text())
    if test_workers is not None and config["repo"] != "ponylang/ponyc":
        raise ValueError("Native test workers are supported only for Ponyc")
    solver = json.loads((args.solver_run / "solver.json").read_text())
    predictions = json.loads((args.solver_run / "predictions.json").read_text())
    if (
        len(predictions) != 1
        or predictions[0]["instance_id"] != solver["original_inst_id"]
        or predictions[0]["model_patch"] != solver["model_patch"]
    ):
        raise ValueError("Native Multi prediction differs from the solver patch")
    case = selected_native_case(args.dataset, solver, config)
    for name, sha in config["implementation"].items():
        if digest(args.solver_run / "implementation" / name) != sha:
            raise ValueError("Archived solver implementation changed")
    metadata = json.loads(command(["docker", "image", "inspect", image]))[0]
    if metadata["Id"] == config["image_id"]:
        raise ValueError("Multi solver and native grading images must be separate")
    args.output.mkdir(parents=True, exist_ok=False)
    dataset = args.output / "native-case.jsonl"
    dataset.write_text(json.dumps(case) + "\n")
    patch = args.output / "prediction.patch"
    patch.write_text(solver["model_patch"])
    repo = config["repo"]
    cwd = solver_cwd("Multi", repo)
    scripts = native_script_hashes(case)
    if set(scripts) != {"run.sh", "test-run.sh", "fix-run.sh"}:
        raise ValueError("Pinned native test entrypoints are incomplete")
    work = args.output / "work" / repo / "evals" / f"pr-{case['number']}"
    work.mkdir(parents=True)
    container = "codenib-contextbench-multi-" + uuid.uuid4().hex[:12]
    write_json(
        args.output / "config.json",
        {
            "solver_sha256": digest(args.solver_run / "solver.json"),
            "prediction_sha256": digest(args.solver_run / "predictions.json"),
            "native_case_sha256": digest(dataset),
            "native_harness_revision": HARNESS_REVISION,
            "native_harness_hashes": hashes,
            "image_id": metadata["Id"],
            "image_repo_digests": metadata.get("RepoDigests", []),
            "adapter_sha256": digest(Path(__file__)),
            "timeout": args.timeout,
            "container_start_timeout": 180,
            "source_commit": config["source_commit"],
            "source_tree": config["source_tree"],
            "solver_cwd": cwd,
            "native_script_sha256": scripts,
            "native_resources": {
                "memory_gib": memory_gib,
                "cpu_quota": 2,
                "cpu_affinity": cpu_affinity,
                "pids_limit": pids_limit,
                "network": network,
                "test_workers": test_workers,
                "environment": (
                    {"MAKEFLAGS": "num_cores=2"} if test_workers is not None else {}
                ),
            },
            "scope": "Native Multi development grading only; no model calls",
        },
    )
    row = {
        "instance_id": solver["instance_id"],
        "status": "starting",
        "resolved": False,
        "container": container,
        "model_calls": 0,
    }
    result_path = args.output / "resolution.json"
    write_json(result_path, row)
    started = time.monotonic()
    try:
        command(
            [
                "docker",
                "run",
                "--rm",
                "--detach",
                "--name",
                container,
                "--network",
                network,
                "--memory",
                f"{memory_gib}g",
                "--cpus",
                "2",
                "--pids-limit",
                str(pids_limit),
                *(["--cpuset-cpus", cpu_affinity] if cpu_affinity is not None else []),
                *(
                    ["--env", "MAKEFLAGS=num_cores=2"]
                    if test_workers is not None
                    else []
                ),
                metadata["Id"],
                "sleep",
                str(args.timeout + 240),
            ],
            timeout=180,
        )
        snapshot = command(
            [
                "docker",
                "exec",
                "-w",
                cwd,
                container,
                "bash",
                "-c",
                "git rev-parse HEAD HEAD^{tree} && "
                "git status --porcelain --untracked-files=no",
            ]
        )
        if snapshot.splitlines() != [config["source_commit"], config["source_tree"]]:
            raise ValueError("Native Multi image has different or dirty source")
        row["source_snapshot_verified"] = True
        row["native_patch_sha256"] = {}
        for field, path in (
            ("fix_patch", "/home/fix.patch"),
            ("test_patch", "/home/test.patch"),
        ):
            actual = command(["docker", "exec", container, "sha256sum", path]).split()[
                0
            ]
            if actual != hashlib.sha256(case[field].encode()).hexdigest():
                raise ValueError("Native image patch differs from selected dataset")
            row["native_patch_sha256"][field] = actual
        row["native_script_sha256"] = {}
        for name, expected in scripts.items():
            actual = command(
                ["docker", "exec", container, "sha256sum", f"/home/{name}"]
            ).split()[0]
            if actual != expected:
                raise ValueError("Native test script differs from pinned harness")
            row["native_script_sha256"][name] = actual
        command(["docker", "cp", str(patch.resolve()), f"{container}:/home/fix.patch"])
        actual = command(
            ["docker", "exec", container, "sha256sum", "/home/fix.patch"]
        ).split()[0]
        if actual != digest(patch):
            raise ValueError("Native image did not receive the exact solver patch")
        row["applied_solver_patch_sha256"] = actual
        write_json(result_path, row)
        with (work / "fix-patch-run.log").open("x") as log:
            process = subprocess.run(
                ["docker", "exec", container, "bash", "/home/fix-run.sh"],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
            )
        row["native_exit_code"] = process.returncode
        with (args.output / "grading.log").open("x") as log:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "multi_swe_bench.harness.gen_report",
                    "--mode",
                    "evaluation",
                    "--workdir",
                    str(args.output / "work"),
                    "--output_dir",
                    str(args.output / "grade"),
                    "--dataset_files",
                    str(dataset),
                    "--max_workers",
                    "1",
                    "--log_dir",
                    str(args.output / "grade-logs"),
                    "--log_to_console",
                    "false",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=True,
            )
        report = json.loads((args.output / "grade/final_report.json").read_text())
        if report["total_instances"] != 1 or report["submitted_ids"] != [
            f"{repo}:pr-{case['number']}"
        ]:
            raise ValueError("Native Multi grader did not retain exactly one task")
        if report["completed_instances"] != 1 or report["error_instances"]:
            row["native_report"] = report
            raise ValueError("Native Multi grader did not complete the selected task")
        row.update(
            status="graded",
            resolved=report["resolved_instances"] == 1,
            native_report=report,
            native_log_sha256=digest(work / "fix-patch-run.log"),
        )
    except Exception as exc:
        row.update(status="error", resolved=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        row["container_removed"] = False
        try:
            removal = subprocess.run(
                ["docker", "rm", "-f", container], capture_output=True, timeout=60
            )
            row["container_removed"] = removal.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            row["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if not row["container_removed"]:
            row.update(status="error", resolved=False)
            row.setdefault("cleanup_error", "Container removal failed")
        row["wall_s"] = time.monotonic() - started
        write_json(result_path, row)
    print(json.dumps({key: row[key] for key in ("instance_id", "status", "resolved")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver-run", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-image", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--native-memory-gib", type=int, choices=(3, 8), default=3)
    parser.add_argument("--native-network", choices=("none", "bridge"), default="none")
    parser.add_argument(
        "--native-test-workers",
        type=int,
        choices=(2,),
        help="Bound Ponyc full-program parallelism; keep native test selectors",
    )
    parser.add_argument(
        "--native-cpu-count",
        type=int,
        choices=(2,),
        help="Expose two allowed CPUs to native worker pools; retain CPU quota",
    )
    parser.add_argument(
        "--native-pids-limit",
        type=int,
        choices=(256, 1024),
        default=256,
        help="Explicit native process ceiling for admitted JVM test pools",
    )
    grade_multi(parser.parse_args())


if __name__ == "__main__":
    main()
