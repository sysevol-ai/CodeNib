# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for reusable baseline task/result envelopes."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from codenib.eval.agent_runner.baseline import (
    BaselineRunResult,
    BaselineTask,
    build_baseline_result_entry,
    location_symbol_ids,
    unique_location_files,
)
from codenib.eval.agent_runner.loc_baseline import (
    build_result_entry,
    run_agent_baseline,
)


def _success_result():
    return SimpleNamespace(
        success=True,
        repo_path="/repos/demo",
        locations=[
            SimpleNamespace(
                name="Parser.parse()",
                type="method",
                file_path="./pkg/parser.py",
                line_start=10,
                line_end=20,
                action="modify",
                description="parse entry point",
            ),
            {
                "name": "helper()",
                "type": "function",
                "file_path": "pkg/parser.py",
                "line_start": "30",
                "line_end": "32",
                "action": "modify",
                "description": "helper path",
            },
        ],
        usage={"input_tokens": 10},
    )


def test_baseline_task_from_dataset_instance_builds_agent_call():
    task = BaselineTask.from_dataset_instance(
        {
            "instance_id": "demo__repo-1",
            "repo": "demo/repo",
            "problem_statement": "Fix parser edge case",
            "hints_text": "Look at parser helpers",
        },
        repo_path="/repos/demo",
        target_files=["pkg/parser.py"],
        target_symbols=["pkg/parser.py:Parser.parse()"],
        metadata={"dataset": "codenib_base"},
    )

    assert task.to_agent_call() == {
        "query_text": "Fix parser edge case",
        "repo_path": "/repos/demo",
        "context": {
            "issue_title": "[demo__repo-1] demo/repo",
            "issue_body": "Fix parser edge case",
            "hints": "Look at parser helpers",
        },
    }
    assert task.to_dict()["target_files"] == ["pkg/parser.py"]
    assert task.to_dict()["metadata"] == {"dataset": "codenib_base"}


def test_baseline_run_result_normalizes_locations_and_predictions():
    normalized = BaselineRunResult.from_raw(_success_result())

    assert normalized.success is True
    assert normalized.usage == {"input_tokens": 10}
    assert [location.to_dict() for location in normalized.locations] == [
        {
            "name": "Parser.parse()",
            "type": "method",
            "file_path": "./pkg/parser.py",
            "line_start": 10,
            "line_end": 20,
            "action": "modify",
            "description": "parse entry point",
        },
        {
            "name": "helper()",
            "type": "function",
            "file_path": "pkg/parser.py",
            "line_start": 30,
            "line_end": 32,
            "action": "modify",
            "description": "helper path",
        },
    ]
    assert unique_location_files(normalized.locations) == [
        "./pkg/parser.py",
        "pkg/parser.py",
    ]
    assert location_symbol_ids(normalized.locations) == [
        "pkg/parser.py:Parser.parse()",
        "pkg/parser.py:helper()",
    ]


def test_build_baseline_result_entry_matches_existing_json_shape():
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix parser edge case",
        repo_path="/repos/demo",
        target_files=("pkg/parser.py",),
        target_symbols=("pkg/parser.py:Parser.parse()",),
    )

    entry = build_baseline_result_entry(
        task=task,
        agent_name="external",
        model="model-a",
        result=_success_result(),
        metrics_k=[1, 2],
    )

    assert entry["instance_id"] == "demo__repo-1"
    assert entry["agent"] == "external"
    assert entry["model"] == "model-a"
    assert entry["success"] is True
    assert entry["metric_k_files"] == ["./pkg/parser.py", "pkg/parser.py"]
    assert entry["metric_k_node_ids"] == [
        "pkg/parser.py:Parser.parse()",
        "pkg/parser.py:helper()",
    ]
    assert entry["metrics"]["symbols"][1]["accuracy"] == 1.0
    assert entry["usage"] == {"input_tokens": 10}
    assert entry["error"] is None


def test_loc_agent_runner_build_result_entry_delegates_to_envelope():
    entry = build_result_entry(
        instance_id="demo__repo-1",
        agent_name="claude",
        model="sonnet",
        result=_success_result(),
        target_files=["pkg/parser.py"],
        target_symbols=["pkg/parser.py:Parser.parse()"],
        metrics_k=[1],
    )

    assert entry["agent"] == "claude"
    assert entry["metric_k_node_ids"] == [
        "pkg/parser.py:Parser.parse()",
        "pkg/parser.py:helper()",
    ]
    assert entry["locations"][0]["name"] == "Parser.parse()"


def test_loc_agent_runner_legacy_import_warns_deprecated():
    sys.modules.pop("codenib.eval.loc_agent_runner", None)

    with pytest.warns(DeprecationWarning, match="agent_runner.loc_baseline"):
        module = importlib.import_module("codenib.eval.loc_agent_runner")

    assert module.run_agent_baseline is run_agent_baseline
    assert "deprecated" in module._DEPRECATION_MESSAGE


def test_build_baseline_result_entry_handles_failures():
    task = BaselineTask(
        instance_id="demo__repo-1",
        query="Fix parser edge case",
        repo_path="/repos/demo",
    )

    entry = build_baseline_result_entry(
        task=task,
        agent_name="lsp",
        model=None,
        result=None,
        metrics_k=[1],
        error="tool unavailable",
    )

    assert entry["success"] is False
    assert entry["error"] == "tool unavailable"
    assert entry["metric_k_files"] == []
    assert entry["metrics"] is None


def test_run_agent_baseline_uses_baseline_task_envelope(monkeypatch, tmp_path):
    calls = []
    instance = {
        "instance_id": "demo__repo-1",
        "repo": "demo/repo",
        "problem_statement": "Fix parser edge case",
        "hints_text": "Look at parser helpers",
    }

    class FakeDataset:
        simplified_symbols = True

        def load(self):
            return [instance]

        def load_eval_metadata(self, _path):
            return {
                "demo__repo-1": {
                    "target_files": ["pkg/parser.py"],
                    "symbols_modified": ["pkg/parser.py:Parser.parse()"],
                    "symbols_deleted": [],
                }
            }

        def process_instance(self, _instance):
            return None

        def get_repo_path(self, _instance):
            return "/repos/demo"

    class FakeAgent:
        async def locate_code(self, query_text, repo_path, context):
            calls.append(
                {
                    "query_text": query_text,
                    "repo_path": repo_path,
                    "context": context,
                }
            )
            return _success_result()

    monkeypatch.setattr(
        "codenib.eval.agent_runner.loc_baseline.build_dataset",
        lambda _args: FakeDataset(),
    )
    args = SimpleNamespace(
        dataset="codenib_base",
        split="test",
        filter_instance=".*",
        repo_cache_dir="",
        result_path=str(tmp_path / "baseline.jsonl"),
        resume=False,
        limit=None,
        eval_instances="",
        metrics_k=[1],
    )

    rc = asyncio.run(
        run_agent_baseline(
            args,
            agent_factory=FakeAgent,
            agent_name="fake",
            model_label="model-a",
        )
    )

    assert rc == 0
    assert calls == [
        {
            "query_text": "Fix parser edge case",
            "repo_path": "/repos/demo",
            "context": {
                "issue_title": "[demo__repo-1] demo/repo",
                "issue_body": "Fix parser edge case",
                "hints": "Look at parser helpers",
            },
        }
    ]
    rows = [
        json.loads(line)
        for line in (tmp_path / "baseline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["instance_id"] == "demo__repo-1"
    assert rows[0]["agent"] == "fake"
    assert rows[0]["success"] is True


def test_run_agent_baseline_resume_skips_processing_done_instances(
    monkeypatch, tmp_path
):
    calls = []
    instances = [
        {
            "instance_id": "demo__repo-1",
            "repo": "demo/repo",
            "problem_statement": "Already done",
        },
        {
            "instance_id": "demo__repo-2",
            "repo": "demo/repo",
            "problem_statement": "Fix parser edge case",
        },
    ]

    class FakeDataset:
        simplified_symbols = True

        def __init__(self):
            self.processed = []

        def load(self):
            return list(instances)

        def load_eval_metadata(self, _path):
            return {
                "demo__repo-1": {
                    "target_files": ["pkg/old.py"],
                    "symbols_modified": ["pkg/old.py:old()"],
                    "symbols_deleted": [],
                },
                "demo__repo-2": {
                    "target_files": ["pkg/parser.py"],
                    "symbols_modified": ["pkg/parser.py:Parser.parse()"],
                    "symbols_deleted": [],
                },
            }

        def process_instance(self, instance):
            self.processed.append(instance["instance_id"])

        def get_repo_path(self, instance):
            return f"/repos/{instance['instance_id']}"

    dataset = FakeDataset()

    class FakeAgent:
        async def locate_code(self, query_text, repo_path, context):
            calls.append(
                {
                    "query_text": query_text,
                    "repo_path": repo_path,
                    "context": context,
                }
            )
            return _success_result()

    monkeypatch.setattr(
        "codenib.eval.agent_runner.loc_baseline.build_dataset",
        lambda _args: dataset,
    )
    result_path = tmp_path / "baseline.jsonl"
    result_path.write_text(
        json.dumps({"instance_id": "demo__repo-1", "success": True}) + "\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        dataset="codenib_base",
        split="test",
        filter_instance=".*",
        repo_cache_dir="",
        result_path=str(result_path),
        resume=True,
        limit=None,
        eval_instances="",
        metrics_k=[1],
    )

    rc = asyncio.run(
        run_agent_baseline(
            args,
            agent_factory=FakeAgent,
            agent_name="fake",
            model_label="model-a",
        )
    )

    assert rc == 0
    assert dataset.processed == ["demo__repo-2"]
    assert calls == [
        {
            "query_text": "Fix parser edge case",
            "repo_path": "/repos/demo__repo-2",
            "context": {
                "issue_title": "[demo__repo-2] demo/repo",
                "issue_body": "Fix parser edge case",
            },
        }
    ]
    rows = [json.loads(line) for line in result_path.read_text().splitlines()]
    assert [row["instance_id"] for row in rows] == [
        "demo__repo-1",
        "demo__repo-2",
    ]
