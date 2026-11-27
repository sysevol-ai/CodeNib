# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Exercise the subprocess boundary without Docker, datasets or model calls."""

import json
import subprocess

import pytest

from codenib.eval.benchmarks.swe_milestone_harness import (
    file_sha256,
    score_snapshot,
    verify_checkout,
)


@pytest.fixture
def harness(tmp_path):
    checkout = tmp_path / "harness checkout"
    package = checkout / "harness/e2e"
    package.mkdir(parents=True)
    (checkout / "harness/__init__.py").touch()
    (package / "__init__.py").touch()
    (package / "evaluator.py").write_text(
        "import json, pathlib, sys\n"
        'output = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])\n'
        'output.write_text(json.dumps({"resolved": False}))\n'
        "sys.exit(1)\n"
    )
    (checkout / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    return checkout, commit


def test_wrong_revision_or_modified_evaluator_is_rejected(harness):
    checkout, commit = harness
    with pytest.raises(ValueError, match="differs"):
        verify_checkout(checkout, "0" * 40)
    (checkout / "harness/e2e/evaluator.py").write_text("raise RuntimeError()\n")
    with pytest.raises(ValueError, match="local changes"):
        verify_checkout(checkout, commit)


def test_unresolved_grade_retains_policy_and_snapshot_receipts(harness, tmp_path):
    checkout, commit = harness
    snapshot = tmp_path / "source snapshot.tar"
    config = tmp_path / "repo_config.yaml"
    classification = tmp_path / "classification.json"
    runtime_policy = tmp_path / "runtime_policy.yaml"
    snapshot.write_bytes(b"saved submission")
    config.write_text("prune_start_guard_exempt: [db/migrations]\n")
    classification.write_text("{}")
    runtime_policy.write_text("{}\n")
    (tmp_path / "metadata.json").write_text("{}\n")
    destination = tmp_path / "score"
    receipt = score_snapshot(
        checkout=checkout,
        commit=commit,
        workspace=tmp_path,
        milestone="milestone_003_sub-01",
        snapshot=snapshot,
        classification=classification,
        repo_config=config,
        output_dir=destination,
        runtime_policy=runtime_policy,
        runtime_policy_mode="protected",
    )
    assert receipt["returncode"] == 1
    assert receipt["input_sha256"]["snapshot"] == file_sha256(snapshot)
    assert receipt["settings"]["repo_config_sha256"] == file_sha256(config)
    assert "--fail-closed-build-reports" in receipt["command"]
    assert receipt["settings"]["runtime_policy_sha256"] == file_sha256(runtime_policy)
    assert "--runtime-policy" in receipt["command"]
    assert json.loads((destination / "evaluation_result.json").read_text()) == {
        "resolved": False
    }
    with pytest.raises(FileExistsError):
        score_snapshot(
            checkout=checkout,
            commit=commit,
            workspace=tmp_path,
            milestone="milestone_003_sub-01",
            snapshot=snapshot,
            classification=classification,
            repo_config=config,
            output_dir=destination,
        )


def test_incomplete_runtime_policy_cannot_start_grading(harness, tmp_path):
    checkout, commit = harness
    with pytest.raises(ValueError, match="together"):
        score_snapshot(
            checkout=checkout,
            commit=commit,
            workspace=tmp_path,
            milestone="milestone_003_sub-01",
            snapshot=tmp_path / "unused",
            classification=tmp_path / "unused",
            repo_config=tmp_path / "unused",
            output_dir=tmp_path / "must_not_exist",
            runtime_policy_mode="protected",
        )
    assert not (tmp_path / "must_not_exist").exists()
