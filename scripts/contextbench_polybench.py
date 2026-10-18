#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Native PolyBench grading with pinned repository-specific official parsers.

Use the pinned official CSV, parser and scorer. Native reference data stay in
this separate grader, never the solver. The canonical corpus has eleven supported
repository mappings; source-image and reference controls remain per-dispatch gates.
"""

from __future__ import annotations

import argparse
import ast
import csv
import dataclasses
import hashlib
import importlib
import io
import json
import re
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.contextbench_native_repos import POLY_REPOS

HARNESS_REVISION = "9c836c5d7f3cb991934132b77d29e6941d912a07"
DATASET_REVISION = "d56445f9940eae4e9d2974ec66820c2f1d7754e6"
DATASET_SHA256 = "17ad661b20e9af1e2067fbbc2e2658a21137d56693570d1d07f966d8bc0408b7"
CL100K_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
CL100K_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
CL100K_CACHE_DIR = "/tmp/codenib-tiktoken-cache"
HARNESS_HASHES = {
    "__init__.py": "d76678eeac19673628ed624db32a20db823ca4005b9d499141424ce236cf1452",
    "docker_utils.py": "0b4d763b063b306be0ff1f348740a3bcdfd8b60457bb94a6e5bf400d6056344c",
    "constants.py": "eb2a9f71dfa1f08a551587c8b3518caf28b1e6a5a5c800511c19eeb019826ad5",
    "polybench_data.py": "7849a1989af5dc391c43ae488ae5efee9120fe58f2a1484bbb65d6ff63836635",
    "scoring.py": "5d5f20fa8a05942908fff849d1797ed4a0bf18ce1a37cae419c2fdd3b0a14efc",
    "parsers/__init__.py": "ebcfd3ca92c588b84e2f39ef8dc999d518f4069d0a21c9b850e1bed7cfd7ac3b",
    "parsers/java_parsers.py": (
        "b35d5792e85ac0523511e3d9f225d9b7c80975cc1f1033836ee4451a1c4f9d0c"
    ),
    "parsers/javascript_parsers.py": (
        "4a83ceab7802e4c187dece23a9d9421b81b56181fa8cdd4812944d6b7646b8a2"
    ),
    "parsers/typescript_parsers.py": (
        "0631e333c9162bfbb9bca1fa84654e259e9db09eeb41f2d57490a3ba46edd4ba"
    ),
    "parsers/python_parsers.py": (
        "b0fcf0d2f3c687cff74037fff67e1134219c4d165e14809dfe8c47cc8c3b81bc"
    ),
    "parsers/parser_protocol.py": (
        "1094c1b7b29e2a9089f75b60b158e64e358d5332e8ea13f74c1e2d8199794b27"
    ),
    "parsers/parser_results.py": (
        "29c4d0e26c9cceea18457ee108eb8c747722276705e48a4414c3f6f9a3de7555"
    ),
}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def verified_cl100k_cache(path):
    """Stage one pinned public resource, with no network or source/test overlay.

    Read and authenticate the exact bytes once, then retain that same immutable
    payload for upload. Caller-supplied archive paths or extraction are forbidden.
    The native image and official test/parser/scorer remain unchanged.
    """
    with path.open("rb") as stream:
        content = stream.read(2 * 1024 * 1024 + 1)
    if hashlib.sha256(content).hexdigest() != CL100K_SHA256:
        raise ValueError("Offline cl100k resource differs from its pinned SHA-256")
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        directory = tarfile.TarInfo(Path(CL100K_CACHE_DIR).name)
        directory.type, directory.mode = tarfile.DIRTYPE, 0o755
        archive.addfile(directory)
        filename = directory.name + "/" + hashlib.sha1(CL100K_URL.encode()).hexdigest()
        member = tarfile.TarInfo(filename)
        member.size, member.mode = len(content), 0o644
        archive.addfile(member, io.BytesIO(content))
    return payload.getvalue(), {
        "public_resource_url": CL100K_URL,
        "resource_sha256": CL100K_SHA256,
        "resource_bytes": len(content),
        "container_cache_dir": CL100K_CACHE_DIR,
        "network_calls": 0,
    }


def native_environment(upstream):
    if upstream is None:
        raise ValueError("Poly grading requires --native-upstream")
    root = upstream.resolve()
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=30
    ).strip()
    package = root / "src/poly_bench_evaluation"
    if commit != HARNESS_REVISION or any(
        digest(package / name) != expected for name, expected in HARNESS_HASHES.items()
    ):
        raise ValueError("Native Poly modules differ from the pinned harness")
    sys.path.insert(0, str(root / "src"))
    modules = {
        name: importlib.import_module("poly_bench_evaluation." + name)
        for name in ["docker_utils", "parsers", "scoring", "constants"]
    }
    if any(
        not Path(module.__file__).resolve().is_relative_to(package.resolve())
        for module in modules.values()
    ):
        raise ValueError("Native Poly import resolves outside the pinned checkout")
    return SimpleNamespace(
        manager=modules["docker_utils"].DockerManager,
        parser=modules["parsers"].JavascriptGenericParser,
        parser_classes={
            repo: getattr(
                modules["parsers"], modules["constants"].REPO_TO_PARSER_CLASS[repo]
            )
            for repo in POLY_REPOS
        },
        score=modules["scoring"].instance_level_scoring,
        hashes=HARNESS_HASHES,
    )


def selected_native_case(path, solver, config):
    """Authenticate the whole native artifact before interpreting any row."""
    if digest(path) != DATASET_SHA256:
        raise ValueError("Native Poly CSV differs from its pinned dataset revision")
    # Some unselected native patches exceed the stdlib's default field limit.
    # This synchronous grader owns parsing; restore the process setting before
    # the upstream Docker worker can start.
    previous_limit = csv.field_size_limit(10 * 1024 * 1024)
    try:
        with path.open(newline="") as stream:
            selected = [
                row
                for row in csv.DictReader(stream)
                if row["instance_id"] == solver["original_inst_id"]
            ]
    finally:
        csv.field_size_limit(previous_limit)
    if len(selected) != 1:
        raise ValueError("Native Poly task identity is missing or ambiguous")
    row = selected[0]
    if (
        config.get("source") != "Poly"
        or config.get("repo") not in POLY_REPOS
        or row["repo"] != config["repo"]
        or row["base_commit"] != config["source_commit"]
        or not re.fullmatch(
            re.escape(row["repo"].replace("/", "__")) + r"-[1-9][0-9]*",
            row["instance_id"],
        )
    ):
        raise ValueError("Native Poly task differs from the supported source snapshot")
    f2p, p2p = ast.literal_eval(row["F2P"]), ast.literal_eval(row["P2P"])
    if not f2p or any(
        not isinstance(group, list) or any(not isinstance(name, str) for name in group)
        for group in [f2p, p2p]
    ):
        raise ValueError("Native Poly test identities are invalid")
    return row, f2p, p2p


def parser_for_repo(native, repo):
    """Use the pinned upstream repository parser rather than a global JS parser."""
    if hasattr(native, "parser_classes"):
        return native.parser_classes[repo]
    if repo == "sveltejs/svelte":
        # Retain the existing Svelte pilot harness interface.
        return native.parser
    raise ValueError("Native Poly parser metadata is unavailable for this repository")


def validated_electron_cache(metadata, original, case, config, instance_id, proof):
    """Bind an explicit Electron cache to its separate original task image.

    Preparation attests unchanged tracked source/tests. Actual image identity,
    task provenance, layer ancestry and execution settings are checked here.
    """
    prefix = (
        "ghcr.io/timesler/swe-polybench.eval.x86_64."
        + case["instance_id"].lower()
        + "@sha256:"
    )
    required_true = (
        "owned_container_removed",
        "all_tracked_source_and_test_bytes_unchanged",
        "original_source_and_native_images_unchanged",
        "native_command_parser_scorer_and_limits_unchanged",
        "network_enabled_only_during_preparation",
    )
    if (
        case["repo"] != "microsoft/vscode"
        or proof.get("status") != "prepared"
        or proof.get("instance_id") != instance_id
        or proof.get("original_native_image_id") != original["Id"]
        or proof.get("derived_native_image_id") != metadata["Id"]
        or proof.get("source_image_unchanged") != config["image_id"]
        or proof.get("source_commit") != config["source_commit"]
        or proof.get("source_commit") != case["base_commit"]
        or proof.get("source_tree") != config["source_tree"]
        or original["Id"] == config["image_id"]
        or metadata["Id"] in {original["Id"], config["image_id"]}
        or not all(proof.get(name) is True for name in required_true)
        or not any(
            value.startswith(prefix) for value in original.get("RepoDigests", [])
        )
    ):
        raise ValueError("Electron cache proof differs from its task or source image")
    version, checksum = proof.get("pinned_Electron_version"), proof.get(
        "executable_sha256"
    )
    if (
        not isinstance(version, str)
        or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version)
        or proof.get("embedded_executable_Electron_version") != version
        or not isinstance(checksum, str)
        or not re.fullmatch(r"[a-f0-9]{64}", checksum)
    ):
        raise ValueError("Electron cache executable/version proof is incomplete")
    old_layers = original.get("RootFS", {}).get("Layers", [])
    new_layers = metadata.get("RootFS", {}).get("Layers", [])
    if (
        not old_layers
        or len(new_layers) <= len(old_layers)
        or new_layers[: len(old_layers)] != old_layers
    ):
        raise ValueError("Electron cache does not extend its original image layers")
    settings = (
        "Cmd",
        "Entrypoint",
        "Env",
        "WorkingDir",
        "User",
        "ExposedPorts",
        "Volumes",
        "Shell",
        "StopSignal",
        "Healthcheck",
    )

    def setting(image, key):
        value = image["Config"].get(key)
        # Docker may represent the absence of an entrypoint as null or [].
        return None if key == "Entrypoint" and value in (None, []) else value

    if any(setting(metadata, k) != setting(original, k) for k in settings):
        raise ValueError("Electron cache changes native execution configuration")
    return {
        "kind": "native-only-public-Electron-cache-v1",
        "original_native_image_id": original["Id"],
        "derived_native_image_id": metadata["Id"],
        "pinned_Electron_version": version,
        "executable_sha256": checksum,
    }


def grade_poly(args):
    """Own one bounded grader and verify removal before returning its result.

    Only this invocation creates the unique labeled container. Its successful
    removal is the completion boundary; root owns recovery from a cleanup error.
    A bounded sleep terminates an orphan after owner death. Source-only solver
    images remain separate; no host mount, network or provider input is supplied.
    """
    if not 1 <= args.timeout <= 3600:
        raise ValueError("Native Poly timeout must be between 1 and 3600 seconds")
    image = getattr(args, "native_image", None)
    if not image or not re.fullmatch(r"(?:sha256:|.+@sha256:)[a-f0-9]{64}", image):
        raise ValueError("Poly grading needs a separate pinned --native-image")
    cache_path = getattr(args, "cl100k_cache_file", None)
    cache = verified_cl100k_cache(cache_path) if cache_path is not None else None
    native = native_environment(getattr(args, "native_upstream", None))
    config = json.loads((args.solver_run / "config.json").read_text())
    solver = json.loads((args.solver_run / "solver.json").read_text())
    predictions = json.loads((args.solver_run / "predictions.json").read_text())
    if (
        len(predictions) != 1
        or predictions[0]["instance_id"] != solver["original_inst_id"]
        or predictions[0]["model_patch"] != solver["model_patch"]
    ):
        raise ValueError("Native Poly prediction differs from the solver patch")
    case, f2p, p2p = selected_native_case(args.dataset, solver, config)
    for name, expected in config["implementation"].items():
        if digest(args.solver_run / "implementation" / name) != expected:
            raise ValueError("Archived solver implementation changed")

    import docker

    client = docker.from_env(timeout=120)
    metadata = client.images.get(image).attrs
    prefix = (
        "ghcr.io/timesler/swe-polybench.eval.x86_64."
        + case["instance_id"].lower()
        + "@sha256:"
    )
    if (
        metadata["Id"] == config["image_id"]
        or metadata["Config"]["WorkingDir"].rstrip("/") != "/testbed"
    ):
        raise ValueError("Native Poly image differs from its separate task image")
    electron_path = getattr(args, "electron_cache_proof", None)
    electron_profile, proof_bytes = None, None
    if electron_path is not None:
        with electron_path.open("rb") as stream:
            proof_bytes = stream.read(65537)
        if len(proof_bytes) > 65536:
            raise ValueError("Electron cache proof exceeds its metadata size bound")
        proof = json.loads(proof_bytes)
        origin = (
            proof.get("original_native_image_id") if isinstance(proof, dict) else None
        )
        if not isinstance(origin, str) or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", origin
        ):
            raise ValueError("Electron cache origin must be a pinned image ID")
        original = client.images.get(origin).attrs
        electron_profile = validated_electron_cache(
            metadata, original, case, config, solver["instance_id"], proof
        )
        electron_profile["proof_sha256"] = hashlib.sha256(proof_bytes).hexdigest()
    elif not any(value.startswith(prefix) for value in metadata.get("RepoDigests", [])):
        raise ValueError("Native Poly image differs from its separate task image")
    args.output.mkdir(parents=True, exist_ok=False)
    if proof_bytes is not None:
        (args.output / "electron-cache-proof.json").write_bytes(proof_bytes)
    write_json(args.output / "native-case.json", case)
    name = "codenib-contextbench-poly-" + uuid.uuid4().hex[:16]
    write_json(
        args.output / "config.json",
        {
            "solver_sha256": digest(args.solver_run / "solver.json"),
            "prediction_sha256": digest(args.solver_run / "predictions.json"),
            "native_dataset_sha256": DATASET_SHA256,
            "native_dataset_revision": DATASET_REVISION,
            "native_case_sha256": digest(args.output / "native-case.json"),
            "native_harness_revision": HARNESS_REVISION,
            "native_harness_hashes": native.hashes,
            "image_id": metadata["Id"],
            "image_repo_digests": metadata["RepoDigests"],
            "source_commit": config["source_commit"],
            "source_tree": config["source_tree"],
            "adapter_sha256": digest(Path(__file__)),
            "timeout": args.timeout,
            "native_call_wall_guard_s": args.timeout,
            "offline_tokenizer_cache": cache[1] if cache is not None else None,
            "native_Electron_cache": electron_profile,
            "scope": "Separate native Poly grading only, no model calls",
        },
    )
    row = {
        "instance_id": solver["instance_id"],
        "status": "starting",
        "resolved": False,
        "container": name,
        "model_calls": 0,
    }
    write_json(args.output / "resolution.json", row)

    class OwnedManager(native.manager):
        def create_container(self):
            self.container = self.client.containers.create(
                image=self.image_id,
                name=name,
                detach=True,
                working_dir="/testbed",
                entrypoint="sleep",
                command=[str(args.timeout + 240)],
                network_disabled=True,
                mem_limit="3g",
                nano_cpus=2_000_000_000,
                pids_limit=256,
                labels={"codenib.contextbench.owner": name},
                **(
                    {"environment": {"TIKTOKEN_CACHE_DIR": CL100K_CACHE_DIR}}
                    if cache is not None
                    else {}
                ),
            )
            self.container.start()
            if cache is not None and not self.container.put_archive("/tmp", cache[0]):
                raise ValueError("Offline cl100k cache upload failed")

    manager = OwnedManager(metadata["Id"], delete_image=False, client=client)
    started = time.monotonic()
    incomplete = False
    try:
        if not solver["model_patch"].strip():
            # This is the official empty-generation outcome, not a test run.
            outcome = native.score(case["instance_id"], {}, f2p, p2p, False, False)
        else:
            manager.create_container()
            head = manager.container.exec_run(
                ["git", "rev-parse", "HEAD", "HEAD^{tree}"], workdir="/testbed"
            )
            if head.exit_code or head.output.decode().strip().splitlines() != [
                config["source_commit"],
                config["source_tree"],
            ]:
                raise ValueError("Native Poly image source differs")
            manager.apply_patch_to_container(case["test_patch"], "test")
            try:
                manager.apply_patch_to_container(solver["model_patch"], "code")
            except ValueError as exc:
                if str(exc) != "Failed to apply patch.":
                    raise
                outcome = native.score(case["instance_id"], {}, f2p, p2p, False, True)
            else:
                native_started = time.monotonic()
                row["native_test_returncode"] = manager.docker_run(
                    case["test_command"], timeout=args.timeout
                )
                row["native_call_wall_s"] = time.monotonic() - native_started
                text = "\n".join(manager.run_logs)
                (args.output / "native-test.log").write_text(text)
                parsed = parser_for_repo(native, case["repo"])(text).parse()
                write_json(args.output / "parsed-tests.json", parsed)
                outcome = native.score(
                    case["instance_id"], parsed, f2p, p2p, True, True
                )
                # The pinned scorer already returns unresolved for an empty
                # parse (for example a candidate that fails to compile). Keep
                # that judgment and the raw parser output without a KeyError.
                observed = set(parsed.get("passed_tests", [])) | set(
                    parsed.get("failed_tests", [])
                )
                row["native_test_collection_observed"] = bool(observed)
                row["missing_required_tests"] = sorted(set(f2p + p2p) - observed)
                footer = "Container exited with status code: " + str(
                    row["native_test_returncode"]
                )
                # Upstream can return partial logs after a timeout, and its
                # scorer does not reject missing P2P names. Preserve that raw
                # judgment, but never publish an incomplete run as resolved.
                incomplete = (
                    row["native_call_wall_s"] >= args.timeout
                    or "Container operation timed out" in manager.run_logs
                    or not manager.run_logs
                    or manager.run_logs[-1] != footer
                    or (outcome.resolved and bool(row["missing_required_tests"]))
                )
        row.update(
            status="error" if incomplete else "graded",
            resolved=outcome.resolved and not incomplete,
            native_report=dataclasses.asdict(outcome),
            native_execution_incomplete=incomplete,
        )
    except Exception as exc:
        row.update(status="error", resolved=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        row["container_removed"] = False
        try:
            manager._cleanup()
            manager.container = None
            try:
                retained = client.containers.get(name)
            except docker.errors.NotFound:
                row["container_removed"] = True
            else:
                if retained.labels.get("codenib.contextbench.owner") != name:
                    raise ValueError("Native Poly cleanup owner differs")
                retained.remove(force=True)
                row["container_removed"] = True
        except Exception as exc:
            row.update(status="error", resolved=False, cleanup_error=str(exc))
        row["wall_s"] = time.monotonic() - started
        write_json(args.output / "resolution.json", row)
    print(json.dumps({key: row[key] for key in ("instance_id", "status", "resolved")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver-run", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--native-image", required=True)
    parser.add_argument("--native-upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--electron-cache-proof",
        type=Path,
        help="Explicit native-only Electron cache provenance for a VSCode task image",
    )
    parser.add_argument(
        "--cl100k-cache-file",
        type=Path,
        help="Optional local public cl100k resource; require its pinned SHA-256",
    )
    grade_poly(parser.parse_args())


if __name__ == "__main__":
    main()
