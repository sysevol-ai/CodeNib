# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from types import SimpleNamespace

from codeminer.eval.agent_runner import lsp_agent_study_runner as runner
from codeminer.eval.agent_runner.lsp_agent_study import LSPAgentStudySpec


def _manifest(subjects):
    payload = {
        "benchmark": "codeminer_base_lsp_agent_ablation",
        "dataset": {
            "fingerprint": "fixture",
            "name": "org/dataset",
            "revision": "a" * 40,
            "split": "test",
        },
        "protocol": LSPAgentStudySpec(reps=1).to_dict(),
        "schema_version": 2,
        "subjects": subjects,
        "summary": {"subject_count": len(subjects)},
    }
    payload["manifest_sha256"] = runner._sha256_json(payload)
    return payload


def _subject(instance_id, repo="org/repo", role="development"):
    return {
        "artifact": {
            "graph_sha256": "graph-sha",
            "lsp_index_sha256": "lsp-sha",
            "profile_id": "profile-id",
            "status": "ready",
        },
        "base_commit": "b" * 40,
        "instance_id": instance_id,
        "language": "go",
        "repo": repo,
        "role": role,
        "snapshot_id": f"snapshot-{instance_id}",
    }


def test_selection_keeps_repository_clusters_in_one_shard():
    manifest = _manifest(
        [
            _subject("a-1", repo="org/a"),
            _subject("a-2", repo="org/a"),
            _subject("b-1", repo="org/b"),
            _subject("c-1", repo="org/c"),
        ]
    )

    first = runner.select_study_subjects(manifest, shard_count=2, shard_index=0)
    second = runner.select_study_subjects(manifest, shard_count=2, shard_index=1)

    first_repos = {row["repo"] for row in first}
    second_repos = {row["repo"] for row in second}
    assert first_repos.isdisjoint(second_repos)
    assert {row["instance_id"] for row in first + second} == {
        "a-1",
        "a-2",
        "b-1",
        "c-1",
    }


def test_manifest_runner_writes_atomic_cells_and_resumes(tmp_path, monkeypatch):
    subject = _subject("org__repo-1")
    manifest = _manifest([subject])
    artifact = tmp_path / "artifacts" / subject["instance_id"]
    artifact.mkdir(parents=True)
    (artifact / "repo").mkdir()
    (artifact / "graph.pkl").write_bytes(b"graph")
    task = SimpleNamespace(
        base_commit=subject["base_commit"],
        instance_id=subject["instance_id"],
        language="go",
        repo=subject["repo"],
        repo_path=str(artifact / "repo"),
        role="development",
    )
    monkeypatch.setattr(runner, "task_from_manifest_subject", lambda *a, **k: task)
    monkeypatch.setattr(
        runner.SCIPOccurrenceIndex,
        "load",
        lambda path: SimpleNamespace(occurrences=()),
    )
    monkeypatch.setattr(
        runner,
        "_prepare_live_provider",
        lambda *a, **k: {"effective_command": ["gopls"], "stable": True},
    )
    calls = []

    class LiveProvider:
        effective_command = ["gopls"]

        def close(self):
            return None

    def run_cell(_task, *, arm, rep, order, **kwargs):
        calls.append((arm, rep, order))
        return {
            "arm": arm,
            "instance_id": subject["instance_id"],
            "lsp_call_count": 0,
            "metrics": {},
            "rep": rep,
            "repo": subject["repo"],
            "role": subject["role"],
            "tool_calls": [],
            "usage": {},
        }

    kwargs = {
        "artifact_root": tmp_path / "artifacts",
        "output_root": tmp_path / "results",
        "llm": SimpleNamespace(model="fake-haiku"),
        "reps": 1,
        "run_mode": "pilot",
        "graph_loader": lambda path: SimpleNamespace(project_root=task.repo_path),
        "live_provider_factory": lambda **kwargs: LiveProvider(),
        "cell_runner": run_cell,
    }
    first = runner.run_lsp_agent_study_manifest(
        manifest,
        {subject["instance_id"]: {}},
        [subject],
        **kwargs,
    )
    second = runner.run_lsp_agent_study_manifest(
        manifest,
        {subject["instance_id"]: {}},
        [subject],
        **kwargs,
    )

    assert first["status"] == second["status"] == "complete"
    assert len(calls) == 3
    assert len(list((tmp_path / "results" / "cells").glob("*.json"))) == 3
    assert not list((tmp_path / "results").rglob("*.tmp"))
    identity = json.loads((tmp_path / "results" / "run.json").read_text())
    assert identity["model"] == "fake-haiku"


def test_error_cells_remain_in_denominator(tmp_path, monkeypatch):
    subject = _subject("org__repo-1")
    manifest = _manifest([subject])
    artifact = tmp_path / "artifacts" / subject["instance_id"]
    artifact.mkdir(parents=True)
    (artifact / "repo").mkdir()
    task = SimpleNamespace(
        instance_id=subject["instance_id"],
        language="go",
        repo_path=str(artifact / "repo"),
    )
    monkeypatch.setattr(runner, "task_from_manifest_subject", lambda *a, **k: task)
    monkeypatch.setattr(
        runner.SCIPOccurrenceIndex,
        "load",
        lambda path: SimpleNamespace(occurrences=()),
    )
    monkeypatch.setattr(
        runner,
        "_prepare_live_provider",
        lambda *a, **k: {"effective_command": ["gopls"], "stable": True},
    )

    class LiveProvider:
        effective_command = ["gopls"]

        def close(self):
            return None

    def fail_live(_task, *, arm, **kwargs):
        if arm == "live_lsp":
            raise RuntimeError("live failed")
        return {
            "arm": arm,
            "instance_id": subject["instance_id"],
            "lsp_call_count": 0,
            "metrics": {},
            "rep": 1,
            "repo": subject["repo"],
            "role": subject["role"],
            "tool_calls": [],
            "usage": {},
        }

    summary = runner.run_lsp_agent_study_manifest(
        manifest,
        {subject["instance_id"]: {}},
        [subject],
        artifact_root=tmp_path / "artifacts",
        output_root=tmp_path / "results",
        llm=SimpleNamespace(model="fake-haiku"),
        reps=1,
        run_mode="pilot",
        graph_loader=lambda path: SimpleNamespace(project_root=task.repo_path),
        live_provider_factory=lambda **kwargs: LiveProvider(),
        cell_runner=fail_live,
    )

    assert summary["status"] == "incomplete"
    assert summary["completed_cell_count"] == 3
    assert summary["error_cell_count"] == 1
