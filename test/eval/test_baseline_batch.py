# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for generic baseline batch execution."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from codenib.eval.agent_runner.baseline import BaselineTask
from codenib.eval.agent_runner.batch import baseline_done_ids, run_baseline_batch


def _task(instance_id: str, *, target_symbol: str = "pkg/mod.py:foo()"):
    return BaselineTask(
        instance_id=instance_id,
        query="Fix foo",
        repo_path="/repo",
        target_files=("pkg/mod.py",),
        target_symbols=(target_symbol,),
    )


def _result(name: str = "foo()"):
    return SimpleNamespace(
        success=True,
        repo_path="/repo",
        locations=[
            SimpleNamespace(
                name=name,
                type="function",
                file_path="pkg/mod.py",
                line_start=1,
                line_end=5,
                action="modify",
                description="candidate",
            )
        ],
        usage={"backend": "fake"},
    )


def test_run_baseline_batch_writes_jsonl_and_aggregates_metrics(tmp_path):
    result_path = tmp_path / "baseline.jsonl"

    async def runner(task):
        assert task.repo_path == "/repo"
        return _result()

    summary = asyncio.run(
        run_baseline_batch(
            [_task("one"), _task("two")],
            runner=runner,
            agent_name="fake",
            model="model-a",
            metrics_k=[1],
            result_path=result_path,
        )
    )

    assert summary.to_dict() == {
        "total": 2,
        "attempted": 2,
        "succeeded": 2,
        "failed": 0,
        "skipped": 0,
        "scored": 2,
        "metrics": {
            "files": {
                1: {
                    "accuracy": 1.0,
                    "precision": 1.0,
                    "recall": 1.0,
                    "avg_hits": 1.0,
                }
            },
            "symbols": {
                1: {
                    "accuracy": 1.0,
                    "precision": 1.0,
                    "recall": 1.0,
                    "avg_hits": 1.0,
                }
            },
        },
        "result_path": str(result_path.resolve()),
    }
    rows = [json.loads(line) for line in result_path.read_text().splitlines()]
    assert [row["instance_id"] for row in rows] == ["one", "two"]
    assert rows[0]["agent"] == "fake"
    assert rows[0]["metric_k_node_ids"] == ["pkg/mod.py:foo()"]


def test_run_baseline_batch_records_runner_exceptions():
    def runner(_task):
        raise RuntimeError("tool unavailable")

    summary = asyncio.run(
        run_baseline_batch(
            [_task("one")],
            runner=runner,
            agent_name="fake",
            model=None,
            metrics_k=[1],
        )
    )

    assert summary.failed == 1
    assert summary.succeeded == 0
    assert summary.scored == 0
    assert summary.metrics["files"][1]["accuracy"] == 0.0


def test_run_baseline_batch_resumes_existing_jsonl(tmp_path):
    result_path = tmp_path / "baseline.jsonl"
    result_path.write_text(
        json.dumps({"instance_id": "one", "success": True}) + "\n",
        encoding="utf-8",
    )

    calls = []

    def runner(task):
        calls.append(task.instance_id)
        return _result()

    summary = asyncio.run(
        run_baseline_batch(
            [_task("one"), _task("two")],
            runner=runner,
            agent_name="fake",
            model=None,
            metrics_k=[1],
            result_path=result_path,
            resume=True,
        )
    )

    assert baseline_done_ids(result_path) == {"one", "two"}
    assert calls == ["two"]
    assert summary.skipped == 1
    assert summary.attempted == 1
    rows = [json.loads(line) for line in result_path.read_text().splitlines()]
    assert [row["instance_id"] for row in rows] == ["one", "two"]
