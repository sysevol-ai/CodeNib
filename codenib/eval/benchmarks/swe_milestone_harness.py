# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Run snapshot grading through an explicitly pinned external harness checkout."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def file_sha256(path: Path) -> str:
    """Hash an artifact without loading the entire snapshot into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_checkout(checkout: Path, commit: str) -> None:
    """Refuse a different revision or local changes to tracked harness code."""
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("evaluator commit must be a full lowercase Git SHA")
    head = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != commit:
        raise ValueError("external evaluator checkout differs from requested commit")
    dirty = subprocess.check_output(
        [
            "git",
            "-C",
            str(checkout),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "harness",
            "pyproject.toml",
        ],
        text=True,
    )
    if dirty.strip():
        raise ValueError("external evaluator code has local changes")
    if not (checkout / "harness/e2e/evaluator.py").is_file():
        raise ValueError("checkout has no SWE-Milestone evaluator")


def score_snapshot(
    *,
    checkout: Path,
    commit: str,
    workspace: Path,
    milestone: str,
    snapshot: Path,
    classification: Path,
    repo_config: Path,
    output_dir: Path,
    allow_partial: bool = False,
    allow_legacy: bool = False,
) -> dict:
    """Grade in a new directory and retain command, policy and artifact hashes.

    The caller must keep the checkout and input files immutable for this call.
    No filename-specific migration repair is applied. The pinned repo config
    owns pruning policy, including ``prune_start_guard_exempt`` where needed.
    Docker lifecycle remains owned by the external evaluator.
    """
    checkout = checkout.resolve()
    verify_checkout(checkout, commit)
    inputs = {
        "snapshot": snapshot.resolve(),
        "classification": classification.resolve(),
        "repo_config": repo_config.resolve(),
    }
    before = {name: file_sha256(path) for name, path in inputs.items()}
    policy = {
        "repo_config_sha256": before["repo_config"],
        "allow_partial_build_reports": allow_partial,
        "allow_legacy_snapshot": allow_legacy,
        "filter_src_only": True,
    }
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    result_path = output_dir / "evaluation_result.json"
    command = [
        sys.executable,
        "-m",
        "harness.e2e.evaluator",
        "--workspace-root",
        str(workspace.resolve()),
        "--milestone-id",
        milestone,
        "--patch-file",
        str(inputs["snapshot"]),
        "--baseline-classification",
        str(inputs["classification"]),
        "--repo-config",
        str(inputs["repo_config"]),
        "--repo-config-sha256",
        before["repo_config"],
        "--output",
        str(result_path),
        (
            "--allow-partial-build-reports"
            if allow_partial
            else "--fail-closed-build-reports"
        ),
    ]
    if allow_legacy:
        command.append("--allow-legacy-snapshot")
    receipt = {
        "evaluator_commit": commit,
        "settings": policy,
        "input_sha256": before,
        "command": command,
        "status": "started",
    }
    receipt_path = output_dir / "invocation.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    try:
        with (output_dir / "evaluator.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(
                command, cwd=checkout, stdout=log, stderr=subprocess.STDOUT, check=False
            )
        receipt["returncode"] = process.returncode
        verify_checkout(checkout, commit)
        if before != {name: file_sha256(path) for name, path in inputs.items()}:
            raise ValueError("evaluation inputs changed during grading")
        # Exit 1 means unresolved, which is a valid evaluation outcome.
        if process.returncode not in (0, 1) or not result_path.is_file():
            raise RuntimeError(f"evaluator failed; see {output_dir / 'evaluator.log'}")
        receipt["result_sha256"] = file_sha256(result_path)
        receipt["status"] = "finished"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt
