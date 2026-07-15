# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.profiling.run_materialized_trace_batch import (
    _is_materialization_checkpoint,
    _run_snapshot,
    _runtime_source_provenance,
    _select_snapshots,
    _write_summary,
)


def test_write_summary_retains_run_provenance(tmp_path):
    run = {"plan_sha256": "abc", "embedding_batch_size": 8}

    _write_summary(
        tmp_path,
        [{"instance_id": "one", "status": "complete"}],
        run=run,
    )

    payload = json.loads((tmp_path / "batch_summary.json").read_text())
    assert payload["run"] == run
    assert payload["counts"] == {"complete": 1}


def test_select_snapshots_preserves_plan_order_and_rejects_unknown_ids():
    snapshots = [{"instance_id": value} for value in ("a", "b", "c")]

    selected = _select_snapshots(snapshots, instance_ids=["c", "a", "c"], limit=None)

    assert [row["instance_id"] for row in selected] == ["a", "c"]
    with pytest.raises(ValueError, match="unknown instance ids: missing"):
        _select_snapshots(snapshots, instance_ids=["missing"], limit=None)


def test_select_snapshots_requires_positive_limit():
    with pytest.raises(ValueError, match="limit must be positive"):
        _select_snapshots([{"instance_id": "a"}], instance_ids=None, limit=0)


def test_runtime_source_provenance_changes_with_source(tmp_path, monkeypatch):
    (tmp_path / "codeminer").mkdir()
    (tmp_path / "scripts" / "profiling").mkdir(parents=True)
    source = tmp_path / "codeminer" / "example.py"
    source.write_text("value = 1\n")
    monkeypatch.setattr(
        "scripts.profiling.run_materialized_trace_batch.subprocess.run",
        lambda *args, **kwargs: type("Result", (), {"stdout": "commit\n"})(),
    )

    before = _runtime_source_provenance(tmp_path)
    source.write_text("value = 2\n")
    after = _runtime_source_provenance(tmp_path)

    assert before["git_commit"] == "commit"
    assert before["file_count"] == 1
    assert before["sha256"] != after["sha256"]


def test_run_snapshot_separates_materialization_and_replay_processes(
    tmp_path, monkeypatch
):
    output_root = tmp_path / "output"
    args = Namespace(
        output_root=output_root,
        embedding_model="model",
        embedding_revision="model-commit",
        embedding_dimension=32,
        embedding_batch_size=8,
        embedding_max_seq_length=1024,
        warmup_repetitions=1,
        measured_repetitions=3,
    )
    snapshot = {
        "instance_id": "instance-a",
        "repo_path": str(tmp_path / "repo"),
        "trace_path": str(tmp_path / "trace.json"),
        "languages": ["python"],
    }
    commands = []

    def fake_run(command, log):
        commands.append(list(command))
        report = Path(command[command.index("--output") + 1])
        if "--materialize-only" in command:
            cache = Path(command[command.index("--cache-dir") + 1])
            cache.mkdir(parents=True)
            (cache / "repo_manifest.json").write_text("{}")
            report.write_text(
                json.dumps(
                    {
                        "status": "materialized",
                        "materialization": {"resource_peaks": {}},
                    }
                )
            )
        else:
            report.write_text(
                json.dumps(
                    {
                        "rows": [{"request_id": "q"}],
                        "materialization": {"wall_seconds": 1.0},
                    }
                )
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        "scripts.profiling.run_materialized_trace_batch._run_logged", fake_run
    )

    result = _run_snapshot(snapshot, args)

    assert result["status"] == "complete"
    assert len(commands) == 2
    assert "--materialize-only" in commands[0]
    assert "--repo-path" in commands[0]
    assert "--manifest" in commands[1]
    assert "--materialization-checkpoint" in commands[1]
    assert commands[0][commands[0].index("--embedding-revision") + 1] == (
        "model-commit"
    )
    assert commands[1][commands[1].index("--embedding-revision") + 1] == (
        "model-commit"
    )
    assert "--repo-path" not in commands[1]


def test_materialization_checkpoint_requires_resource_peaks(tmp_path):
    checkpoint = tmp_path / "report.json"
    checkpoint.write_text(
        json.dumps(
            {
                "status": "materialized",
                "materialization": {"resource_peaks": {}},
            }
        )
    )

    assert _is_materialization_checkpoint(checkpoint)
    checkpoint.write_text(json.dumps({"status": "materialized", "materialization": {}}))
    assert not _is_materialization_checkpoint(checkpoint)
