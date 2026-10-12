# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest

from codenib.dataset.contextbench import (
    ContextBenchCase,
    cases_digest,
    load_contextbench_cases,
)


@pytest.fixture
def task_row():
    return {
        "instance_id": "SWE-Bench-Verified__python__example",
        "original_inst_id": "owner__repo-1",
        "repo": "owner/repo",
        "repo_url": "https://github.com/owner/repo.git",
        "base_commit": "a" * 40,
        "language": "python",
        "source": "Verified",
        "problem_statement": "Fix source loading",
        "gold_context": json.dumps(
            [
                {
                    "file": "src/a.py",
                    "start_line": 2,
                    "end_line": 3,
                    "content": "SECRET GOLD",
                }
            ]
        ),
        "patch": "SECRET PATCH",
        "test_patch": "SECRET TEST PATCH",
    }


def test_load_aliases_filters_and_separates_gold(tmp_path, task_row):
    path = tmp_path / "tasks.jsonl"
    path.write_text(json.dumps(task_row) + "\n")
    cases = load_contextbench_cases(
        path,
        instance_ids=[task_row["original_inst_id"]],
        languages=["python"],
        sources=["verified"],
    )
    assert len(cases) == 1
    assert cases[0].instance_id != cases[0].original_inst_id
    assert "SECRET" not in json.dumps(cases[0].to_record())
    assert cases[0].gold_context[0]["start_line"] == 2
    assert load_contextbench_cases(path, instance_ids=[cases[0].instance_id]) == cases
    with pytest.raises(ValueError, match="unknown"):
        load_contextbench_cases(path, instance_ids=["typo"])
    with pytest.raises(ValueError, match="empty"):
        load_contextbench_cases(path, languages=["go"])


def test_missing_prompt_remains_in_population(task_row):
    task_row["problem_statement"] = None
    assert ContextBenchCase.from_mapping(task_row).problem_statement == ""


@pytest.mark.parametrize(
    "path",
    [
        "/testbed/src/a.py",
        "/workspace/owner__repo__0.1/src/a.py",
        "./src/a.py",
        "/testbed/.github/workflows/ci.yml",
    ],
)
def test_curated_container_paths_are_preserved_for_the_official_scorer(task_row, path):
    task_row["gold_context"] = [{"file": path, "start_line": 1, "end_line": 2}]
    case = ContextBenchCase.from_mapping(task_row)
    assert case.gold_context[0]["file"] == path
    assert ContextBenchCase.from_mapping(case.to_record()) == case


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_id", "../escape"),
        ("base_commit", "HEAD"),
        ("repo", "../../escape"),
        ("repo_url", "file:///secret"),
    ],
)
def test_rejects_unsafe_task_identity(task_row, field, value):
    task_row[field] = value
    with pytest.raises(ValueError):
        ContextBenchCase.from_mapping(task_row)


@pytest.mark.parametrize(
    "span",
    [
        {"file": "../a.py", "start_line": 1, "end_line": 2},
        {"file": "a.py", "start_line": 0, "end_line": 2},
        {"file": "a.py", "start_line": 3, "end_line": 2},
        {"file": "a.py", "start_line": True, "end_line": 2},
    ],
)
def test_rejects_bad_gold_spans(task_row, span):
    task_row["gold_context"] = [span]
    with pytest.raises(ValueError):
        ContextBenchCase.from_mapping(task_row)


def test_duplicate_alias_and_changed_selection_digest(tmp_path, task_row):
    second = {**task_row, "instance_id": "second"}
    path = tmp_path / "data.json"
    path.write_text(json.dumps([task_row, second]))
    with pytest.raises(ValueError, match="duplicate"):
        load_contextbench_cases(path)
    first = ContextBenchCase.from_mapping(task_row)
    second["problem_statement"] = "Different question"
    assert cases_digest([first]) != cases_digest(
        [ContextBenchCase.from_mapping(second)]
    )
