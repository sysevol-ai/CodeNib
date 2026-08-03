# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codenib.eval.agent_runner import lsp_agent_study_runner as runner
from codenib.eval.agent_runner.lsp_agent_study import LSPAgentStudySpec


def _manifest(subjects):
    payload = {
        "benchmark": "codenib_base_lsp_agent_ablation",
        "dataset": {
            "fingerprint": "fixture",
            "name": "org/dataset",
            "revision": "a" * 40,
            "split": "test",
        },
        "protocol": LSPAgentStudySpec(reps=1).to_dict(),
        "schema_version": 3,
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
        "static_native_backend": "scip_occurrence_index_v1",
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


def test_occurrence_index_is_optional_only_for_graph_position_backend(tmp_path):
    assert runner._load_occurrence_index(tmp_path, "cpp") is None
    with pytest.raises(ValueError, match="required LSP occurrence index missing"):
        runner._load_occurrence_index(tmp_path, "python")


def test_cpp_live_readiness_uses_longer_quiet_grace(monkeypatch):
    calls = []

    class Provider:
        language = "cpp"
        effective_command = ["clangd"]

        def start(self):
            return None

        def wait_until_idle(self, **kwargs):
            calls.append(kwargs)
            return True

    monkeypatch.setattr(
        runner, "generate_lsp_replay_requests", lambda *a, **k: [SimpleNamespace()]
    )
    monkeypatch.setattr(
        runner,
        "wait_for_lsp_provider_readiness",
        lambda *a, **k: SimpleNamespace(
            completed_reps=2,
            equivalent_count=1,
            live_nonempty_count=1,
            rows=(),
            stable=True,
            wall_ms=1.0,
        ),
    )

    setup = runner._prepare_live_provider(
        Provider(),
        graph=SimpleNamespace(),
        project_root="/tmp/repo",
        idle_timeout_s=120,
        minimum_reps=2,
        maximum_reps=4,
        poll_seconds=0,
    )

    assert calls == [{"max_wait_s": 120, "idle_grace_s": 10.0}]
    assert setup["idle_grace_s"] == 10.0


def test_manifest_runner_writes_atomic_cells_and_resumes(tmp_path, monkeypatch):
    subject = _subject("org__repo-1")
    manifest = _manifest([subject])
    artifact = tmp_path / "artifacts" / subject["instance_id"]
    artifact.mkdir(parents=True)
    (artifact / "repo").mkdir()
    (artifact / "graph.pkl").write_bytes(b"graph")
    (artifact / "lsp_index.pkl").write_bytes(b"index")
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
    (artifact / "lsp_index.pkl").write_bytes(b"index")
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
