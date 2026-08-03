# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from codenib.eval.benchmarks import swebench_policy_cases
from codenib.eval.benchmarks.swebench_policy_cases import (
    _graph_source_coverage_report,
    _scip_producer,
    _snapshot_errors,
    extract_golden_patch_labels,
    load_dataset_records,
    select_balanced_repository_cases,
)
from codenib.scip_interface.scip_pb2 import Index as SCIPIndex


def test_scip_provenance_records_resolved_tool_and_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        swebench_policy_cases.shutil,
        "which",
        lambda command: "/tools/scip-python" if command == "scip-python" else None,
    )
    monkeypatch.setattr(
        swebench_policy_cases.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="0.6.6\n",
            stderr="",
        ),
    )
    monkeypatch.setenv("CODENIB_SCIP_PYTHON_INDEX_TIMEOUT_SECONDS", "1800")

    assert swebench_policy_cases._scip_python_provenance() == {
        "path": "/tools/scip-python",
        "version": "0.6.6",
        "index_timeout_seconds": "1800",
    }


def test_graph_source_coverage_rejects_missing_or_outside_files() -> None:
    accepted = _graph_source_coverage_report(
        {"a.py", "b.py"},
        {"a.py", "b.py"},
    )
    low_coverage = _graph_source_coverage_report(
        {"a.py", "b.py"},
        {"a.py"},
    )
    outside = _graph_source_coverage_report(
        {"a.py"},
        {"a.py", "generated.py"},
        outside_commit={"generated.py"},
    )

    assert accepted["passed"] is True
    assert low_coverage["passed"] is False
    assert low_coverage["coverage"] == 0.5
    assert outside["passed"] is False
    assert outside["outside_commit_files"] == ["generated.py"]


def test_scip_producer_reads_artifact_metadata(tmp_path: Path) -> None:
    index = SCIPIndex()
    index.metadata.tool_info.name = "scip-python"
    index.metadata.tool_info.version = "0.6.6"
    index.metadata.tool_info.arguments.extend(("index", "--cwd", "/repo"))
    index_path = tmp_path / "index.scip"
    index_path.write_bytes(index.SerializeToString())

    assert _scip_producer(index_path) == {
        "name": "scip-python",
        "version": "0.6.6",
        "arguments": ["index", "--cwd", "/repo"],
    }


def _record(repo: str, index: int) -> dict[str, str]:
    return {
        "repo": repo,
        "instance_id": f"{repo.replace('/', '__')}-{index}",
        "base_commit": f"{index:040x}",
        "patch": f"patch-{index}",
        "problem_statement": f"problem-{index}",
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "parser.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "parser.py")
    _git(repo, "commit", "-m", "base")
    return _git(repo, "rev-parse", "HEAD")


def test_snapshot_errors_rejects_commit_drift_and_dirty_checkout(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    base_commit = _init_repo(repo)

    assert _snapshot_errors(repo, base_commit) == []

    (repo / "parser.py").write_text("value = 2\n", encoding="utf-8")
    assert _snapshot_errors(repo, base_commit) == [
        "snapshot is dirty after index construction"
    ]

    _git(repo, "add", "parser.py")
    _git(repo, "commit", "-m", "change")
    assert _snapshot_errors(repo, base_commit) == [
        f"snapshot commit mismatch: {_git(repo, 'rev-parse', 'HEAD')} != {base_commit}"
    ]


def test_load_dataset_records_accepts_jsonl_and_hashes_source(tmp_path: Path) -> None:
    source = tmp_path / "dataset.jsonl"
    records = [_record("org/a", 1), _record("org/b", 2)]
    source.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    loaded, digest = load_dataset_records(source)

    assert loaded == records
    assert len(digest) == 64


def test_balanced_selection_is_deterministic_and_covers_every_repo() -> None:
    records = [
        *(_record("org/small", index) for index in range(2)),
        *(_record("org/medium", index + 10) for index in range(5)),
        *(_record("org/large", index + 20) for index in range(20)),
    ]

    first, quotas = select_balanced_repository_cases(
        records,
        count=10,
        seed="fixed",
    )
    second, second_quotas = select_balanced_repository_cases(
        reversed(records),
        count=10,
        seed="fixed",
    )

    assert [row["instance_id"] for row in first] == [
        row["instance_id"] for row in second
    ]
    assert quotas == second_quotas
    assert quotas == {"org/large": 4, "org/medium": 4, "org/small": 2}
    assert Counter(row["repo"] for row in first) == Counter(quotas)


def test_extract_golden_patch_labels_restores_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source = repo / "parser.py"
    source.write_text(
        "def parse(value):\n    return value.strip()\n",
        encoding="utf-8",
    )
    _git(repo, "add", "parser.py")
    _git(repo, "commit", "-m", "base")
    base_commit = _git(repo, "rev-parse", "HEAD")
    source.write_text(
        "def parse(value):\n    return value.strip().lower()\n",
        encoding="utf-8",
    )
    patch = _git(repo, "diff", "--", "parser.py")
    _git(repo, "restore", "parser.py")

    labels = extract_golden_patch_labels(
        repo,
        {
            "base_commit": base_commit,
            "patch": patch,
        },
    )

    assert labels["gold_files"] == ["parser.py"]
    assert labels["gold_functions"] == ["parser.py:parse()"]
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == base_commit
