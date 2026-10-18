# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Replay archived source footprints and official metrics without model calls.

Use an extracted bundle with archive-index.json/verification.json beside logs/
and trajectories/. Repositories must be exact clean pinned snapshots; optional
--prepare-repos uses the same clone/submodule preparation as the original run.
Only derived path labels/evaluation inputs change; original evidence is untouched.
"""

import argparse
import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_path(root, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Expected a relative archive member")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Archive member escapes its destination")
    return path


def verify_extracted(bundle):
    index = load(bundle / "archive-index.json")
    verified = load(bundle / "verification.json")
    assert verified["archive_index_sha256"] == sha(bundle / "archive-index.json")
    seen = set()
    for stream in index["streams"].values():
        for member in stream["members"]:
            assert member["name"] not in seen
            seen.add(member["name"])
            path = safe_path(bundle, member["name"])
            assert path.is_file() and not path.is_symlink()
            assert (
                path.stat().st_size == member["bytes"] and sha(path) == member["sha256"]
            )
    assert len(seen) == verified["members_roundtrip_verified"]
    return len(seen)


def unpack_runtime(archive, destination, digest):
    assert sha(archive) == digest
    destination.mkdir(exist_ok=False)
    count = 0
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream:
            path = safe_path(destination, member.name)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            assert member.isfile(), "Runtime archive must not install links/devices"
            path.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output)
            count += 1
    assert count == 1677


def verify_runtime(archive, destination, digest):
    assert sha(archive) == digest
    count = 0
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream:
            if member.isdir():
                continue
            assert member.isfile()
            path = safe_path(destination, member.name)
            assert (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size == member.size
            )
            with stream.extractfile(member) as source:
                assert hashlib.sha256(source.read()).hexdigest() == sha(path)
            count += 1
    assert count == 1677


def acknowledged(messages):
    """Same last-assistant acknowledgement contract as confirmed_history."""
    last = max(
        (i for i, message in enumerate(messages) if message.get("role") == "assistant"),
        default=-1,
    )
    return [
        message
        for i, message in enumerate(messages)
        if message.get("role") != "tool" or i < last
    ]


def rebase_history(messages, original_root, local_root):
    """Translate only source path labels; never edit a source line or original."""
    original_root, local_root = Path(original_root), Path(local_root)

    def path_label(value):
        if not isinstance(value, str) or not Path(value).is_absolute():
            return value
        try:
            relative = Path(value).relative_to(original_root)
        except ValueError:
            return value
        return str(local_root / relative)

    result = copy.deepcopy(messages)
    calls = {}
    for message in result:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                function = call["function"]
                calls[str(call["id"])] = function["name"]
                if function["name"] != "read":
                    continue
                encoded = isinstance(function.get("arguments"), str)
                try:
                    args = (
                        json.loads(function["arguments"])
                        if encoded
                        else function.get("arguments")
                    )
                except ValueError:
                    continue
                if isinstance(args, dict) and "file_path" in args:
                    args["file_path"] = path_label(args["file_path"])
                    function["arguments"] = json.dumps(args) if encoded else args
        if (
            message.get("role") == "tool"
            and calls.get(str(message.get("tool_call_id"))) == "grep"
            and isinstance(message.get("content"), str)
        ):
            lines = []
            for line in message["content"].splitlines(keepends=True):
                match = re.match(r"^(.*?):(\d+): ?", line)
                if match:
                    label = path_label(match[1])
                    line = label + line[len(match[1]) :]
                lines.append(line)
            message["content"] = "".join(lines)
    return result


def replay_case(
    case, case_end, record, raw_by_worker, local_repo, converter, assembler
):
    workers = []
    for label in ["first", "scout"]:
        worker = copy.deepcopy(case_end["workers"][label])
        raw = raw_by_worker[label]
        assert worker["accepted"] and raw["accepted"]
        assert worker["commit"] == case["base_commit"]
        assert (
            worker["source_fingerprint"] == raw["source_context"]["source_fingerprint"]
        )
        assert worker["all_model_usage_usd"] == raw["sdk_reported_cost_usd"]
        history = rebase_history(
            acknowledged(raw["messages"]), record["repo"], local_repo
        )
        pred = converter(
            instance_id=case["instance_id"], messages=history, repo_path=local_repo
        )
        assert (
            pred == worker["prediction"]
        ), "Relocated source footprint differs from original"
        worker["prediction"] = pred
        workers.append(worker)
    return assembler(case["instance_id"], workers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    repositories = parser.add_mutually_exclusive_group(required=True)
    repositories.add_argument("--repos", type=Path)
    repositories.add_argument(
        "--repo-map", type=Path, help="Public task ID to existing exact repository path"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--evaluator-python", default=sys.executable)
    parser.add_argument(
        "--prepare-repos",
        action="store_true",
        help="Fetch pinned public commits/submodules; no model requests",
    )
    args = parser.parse_args()
    assert not args.prepare_repos or args.repos, "--prepare-repos requires --repos"
    bundle = args.bundle.resolve()
    verified_members = verify_extracted(bundle)
    study = bundle / "logs/study"
    plan = load(study / "protocol.json")
    seal = load(study / "sealed-comparison.json")
    analysis = load(study / "analysis.json")
    assert analysis["population"] == plan["population"] == len(plan["cases"]) == 500
    assert analysis["protocol_sha256"] == sha(study / "protocol.json")
    assert analysis["source_seal_sha256"] == sha(study / "sealed-comparison.json")
    runtime = args.runtime.resolve() if args.runtime else bundle / "runtime-extracted"
    if not args.runtime:
        unpack_runtime(
            bundle / "logs/runtime/runtime-source.tar.gz",
            runtime,
            plan["runtime_source_archive_sha256"],
        )
    else:
        verify_runtime(
            bundle / "logs/runtime/runtime-source.tar.gz",
            runtime,
            plan["runtime_source_archive_sha256"],
        )
    sys.path.insert(0, str(runtime))
    from codenib.dataset.contextbench import ContextBenchCase
    from codenib.eval.benchmarks.contextbench import contextbench_prediction
    from codenib.eval.benchmarks.contextbench_runner import (
        audit_contextbench_snapshot,
        contextbench_evaluator_command,
        prepare_contextbench_repo,
    )
    from codenib.source_fingerprint import fingerprint_repository
    from scripts.contextbench_search_pair import assemble_workers

    for name, digest in plan["implementation"].items():
        assert sha(runtime / name) == digest
    upstream = bundle / "logs/pinned-evaluator"
    for name, digest in plan["evaluator_sources"].items():
        assert sha(upstream / name) == digest
    evaluation = load(study / "haiku_scout_pair-evaluation-input.json")
    by_eval = {r["gold"]["inst_id"]: r for r in evaluation}
    sealed = {r["instance_id"]: r for r in seal["arms"]["haiku_scout_pair"]}
    assert len(by_eval) == len(evaluation) == len(sealed) == 500
    repo_map = load(args.repo_map) if args.repo_map else None
    if repo_map is not None:
        assert set(repo_map) == set(sealed)
    derived = []
    args.output.mkdir(parents=True, exist_ok=False)
    for case in plan["cases"]:
        key = case["instance_id"]
        repo = (
            Path(repo_map[key]).resolve(strict=True)
            if repo_map is not None
            else args.repos.resolve() / key
        )
        if args.prepare_repos:
            metadata = {
                k: case[k]
                for k in [
                    "instance_id",
                    "original_inst_id",
                    "repo",
                    "repo_url",
                    "base_commit",
                    "language",
                ]
            }
            repo = prepare_contextbench_repo(
                ContextBenchCase(
                    **metadata, source="", problem_statement="", gold_context=()
                ),
                args.repos,
            )
        audit_contextbench_snapshot(repo, case["base_commit"])
        assert fingerprint_repository(repo).value == plan["source_fingerprints"][key]
        end = load(bundle / "logs/cases" / key / "case-terminal.json")
        record = load(bundle / "logs/inputs" / (key + ".json"))
        raws = {}
        for label in ["first", "scout"]:
            path = bundle / "trajectories" / key / label / "trajectory.json"
            assert sha(path) == end["workers"][label]["raw_sha256"]
            raws[label] = load(path)
        prediction, _ = replay_case(
            case, end, record, raws, repo, contextbench_prediction, assemble_workers
        )
        assert prediction == sealed[key]
        item = copy.deepcopy(by_eval[key])
        item.update(repo_path=str(repo), prediction=prediction)
        derived.append(item)
        print(
            json.dumps({"source_footprint_verified": key, "model_calls": 0}), flush=True
        )
    input_path = args.output / "derived-evaluation-input.json"
    input_path.write_text(json.dumps(derived))
    metrics_path = args.output / "official-metrics.json"
    command = contextbench_evaluator_command(upstream, args.evaluator_python)
    with (args.output / "official-evaluation.log").open("x") as stream:
        result = subprocess.run(
            [*command, "--input", str(input_path), "--output", str(metrics_path)],
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=7200,
        )
    assert result.returncode in (0, 1)
    report = load(metrics_path)
    scored = {r["instance_id"]: r for r in report["cases"]}
    assert len(scored) == len(report["cases"]) == 500
    for case in analysis["candidate"]["cases"]:
        row = scored[case["instance_id"]]
        valid = "error" not in row and "final" in row
        assert valid == case["valid"]
        for level in ["file", "symbol", "line"]:
            r = row["final"][level]["coverage"] if valid else 0
            p = row["final"][level]["precision"] if valid else 0
            for metric, value in {
                "recall": r,
                "precision": p,
                "f1": 2 * r * p / (r + p) if r + p else 0,
            }.items():
                assert math.isclose(
                    value, case["metrics"][level][metric], abs_tol=1e-12
                )
    proof = {
        "extracted_original_members_verified": verified_members,
        "source_footprints_reconstructed": 500,
        "actual_worker_receipts_replayed": 1000,
        "all_case_file_symbol_line_metrics_match": True,
        "original_evidence_modified": False,
        "new_model_calls": 0,
        "native_issue_resolution_evaluated": False,
    }
    (args.output / "reproduction-verification.json").write_text(
        json.dumps(proof, indent=2) + "\n"
    )
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
