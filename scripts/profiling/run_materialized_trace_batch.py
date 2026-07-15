#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run a materialized trace plan with per-snapshot isolation and resume."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--embedding-model", default="Qwen/Qwen3-Embedding-0.6B")
    parser.add_argument("--embedding-revision")
    parser.add_argument("--embedding-dimension", type=int, default=1024)
    parser.add_argument("--embedding-batch-size", type=int, default=8)
    parser.add_argument("--embedding-max-seq-length", type=int, default=8192)
    parser.add_argument("--warmup-repetitions", type=int, default=1)
    parser.add_argument("--measured-repetitions", type=int, default=3)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int)
    selection.add_argument("--instance-id", action="append", dest="instance_ids")
    args = parser.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    snapshots = _select_snapshots(
        plan["snapshots"], instance_ids=args.instance_ids, limit=args.limit
    )
    args.output_root.mkdir(parents=True, exist_ok=True)
    source_provenance = _runtime_source_provenance(ROOT)
    run = {
        "plan_path": str(args.plan.resolve()),
        "plan_sha256": _file_sha256(args.plan),
        "plan_schema_version": plan.get("schema_version"),
        "dataset": plan.get("dataset"),
        "embedding_model": args.embedding_model,
        "embedding_revision": args.embedding_revision,
        "embedding_dimension": args.embedding_dimension,
        "embedding_batch_size": args.embedding_batch_size,
        "embedding_max_seq_length": args.embedding_max_seq_length,
        "warmup_repetitions": args.warmup_repetitions,
        "measured_repetitions": args.measured_repetitions,
        "limit": args.limit,
        "instance_ids": list(args.instance_ids or []),
        "python": sys.executable,
        "source_provenance": source_provenance,
    }
    outcomes = []
    _write_summary(args.output_root, outcomes, run=run)
    for snapshot in snapshots:
        current_source = _runtime_source_provenance(ROOT)
        if current_source["sha256"] != source_provenance["sha256"]:
            outcomes.append(
                {
                    "instance_id": snapshot["instance_id"],
                    "status": "failed",
                    "error": "runtime source changed during batch",
                    "expected_sha256": source_provenance["sha256"],
                    "actual_sha256": current_source["sha256"],
                }
            )
            _write_summary(args.output_root, outcomes, run=run)
            break
        outcomes.append(_run_snapshot(snapshot, args))
        _write_summary(args.output_root, outcomes, run=run)
    return 1 if any(row["status"] == "failed" for row in outcomes) else 0


def _select_snapshots(
    snapshots: Sequence[dict[str, Any]],
    *,
    instance_ids: Sequence[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    selected = list(snapshots)
    if instance_ids:
        requested = list(dict.fromkeys(str(value) for value in instance_ids))
        available = {str(row["instance_id"]): row for row in selected}
        missing = sorted(set(requested) - set(available))
        if missing:
            raise ValueError("unknown instance ids: " + ", ".join(missing))
        requested_set = set(requested)
        selected = [row for row in selected if str(row["instance_id"]) in requested_set]
    elif limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        selected = selected[:limit]
    return selected


def _run_snapshot(snapshot: dict[str, Any], args: argparse.Namespace) -> dict:
    instance_id = snapshot["instance_id"]
    root = args.output_root / instance_id
    cache = root / "cache"
    report = root / "report.json"
    log = root / "run.log"
    root.mkdir(parents=True, exist_ok=True)
    if _is_complete(report):
        return {"instance_id": instance_id, "status": "skipped_complete"}

    benchmark = [
        sys.executable,
        str(Path(__file__).with_name("benchmark_materialized_trace.py")),
    ]
    manifest = cache / "repo_manifest.json"
    shared_arguments = [
        "--trace",
        snapshot["trace_path"],
        "--output",
        str(report),
        "--embedding-model",
        args.embedding_model,
        "--embedding-dimension",
        str(args.embedding_dimension),
        "--embedding-batch-size",
        str(args.embedding_batch_size),
        "--embedding-max-seq-length",
        str(args.embedding_max_seq_length),
    ]
    if args.embedding_revision:
        shared_arguments.extend(["--embedding-revision", args.embedding_revision])
    if not manifest.is_file():
        materialize_command = (
            benchmark
            + [
                "--repo-path",
                snapshot["repo_path"],
                "--cache-dir",
                str(cache),
                "--languages",
                ",".join(snapshot["languages"]),
                "--materialize-only",
            ]
            + shared_arguments
        )
        completed = _run_logged(materialize_command, log)
        if completed.returncode != 0 or not _is_materialization_checkpoint(report):
            return {
                "instance_id": instance_id,
                "status": "failed",
                "stage": "materialization",
                "returncode": completed.returncode,
                "log": str(log),
            }
    elif not _is_materialization_checkpoint(report):
        return {
            "instance_id": instance_id,
            "status": "failed",
            "stage": "materialization",
            "error": "manifest exists without a valid materialization checkpoint",
        }

    replay_command = (
        benchmark
        + [
            "--manifest",
            str(manifest),
            "--materialization-checkpoint",
            str(report),
        ]
        + shared_arguments
        + [
            "--warmup-repetitions",
            str(args.warmup_repetitions),
            "--measured-repetitions",
            str(args.measured_repetitions),
        ]
    )
    completed = _run_logged(replay_command, log)
    if completed.returncode == 0 and _is_complete(report):
        return {"instance_id": instance_id, "status": "complete"}
    return {
        "instance_id": instance_id,
        "status": "failed",
        "stage": "replay",
        "returncode": completed.returncode,
        "log": str(log),
    }


def _run_logged(command: Sequence[str], log: Path) -> subprocess.CompletedProcess:
    with log.open("a", encoding="utf-8") as stream:
        stream.write("COMMAND " + " ".join(command) + "\n")
        stream.flush()
        return subprocess.run(
            command,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
            check=False,
        )


def _is_materialization_checkpoint(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    materialization = payload.get("materialization")
    return bool(
        payload.get("status") == "materialized"
        and isinstance(materialization, dict)
        and isinstance(materialization.get("resource_peaks"), dict)
    )


def _is_complete(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("rows") and payload.get("materialization"))


def _write_summary(
    output_root: Path,
    outcomes: Sequence[dict[str, Any]],
    *,
    run: dict[str, Any],
) -> None:
    payload = {
        "schema_version": 1,
        "run": run,
        "outcomes": list(outcomes),
        "counts": {
            status: sum(row["status"] == status for row in outcomes)
            for status in {row["status"] for row in outcomes}
        },
    }
    (output_root / "batch_summary.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_source_provenance(root: Path) -> dict[str, Any]:
    files = sorted(
        path
        for directory in (root / "codeminer", root / "scripts" / "profiling")
        for path in directory.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        files.append(pyproject)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return {
        "git_commit": commit,
        "sha256": digest.hexdigest(),
        "file_count": len(files),
    }


if __name__ == "__main__":
    raise SystemExit(main())
