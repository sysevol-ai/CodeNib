# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codenib.eval.benchmarks.swe_explore import (
    SWE_EXPLORE_METRICS,
    SWEExploreGroundTruth,
    SWEExploreSourceRecord,
    audit_swe_explore_snapshot,
    flatten_swe_explore_results,
    load_swe_explore_cases,
    load_swe_explore_sources,
    score_swe_explore_regions,
    swe_explore_prediction_record,
)
from codenib.integrations.swe_explore import SWEExploreContextRegion, SWEExploreResult


def _ground_truth() -> SWEExploreGroundTruth:
    return SWEExploreGroundTruth.from_mapping(
        {
            "read_core_files": ["a.py", "b.py"],
            "read_core_regions": [
                {"path": "a.py", "start": 3, "end": 6},
                {"path": "b.py", "start": 1, "end": 2},
            ],
            "read_optional_files_map": {"model": ["a.py"]},
            "read_optional_regions_map": {
                "model": [{"path": "a.py", "start": 8, "end": 9}]
            },
            "modified_core_files": ["a.py"],
            "main_files": ["a.py"],
        }
    )


def test_source_join_normalizes_swebench_pro_instance_prefix(tmp_path: Path) -> None:
    instance_id = "org__repo-deadbeef-v1"
    sources = load_swe_explore_sources(
        {
            "pro": [
                {
                    "instance_id": f"instance_{instance_id}",
                    "repo": "org/repo",
                    "base_commit": "deadbeef",
                    "problem_statement": "Fix the broken parser.",
                }
            ]
        }
    )
    bench = tmp_path / "bench.jsonl"
    bench.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "repo_dir": f"repos/{instance_id}",
                "ground_truth": {
                    "read_core_files": ["src/parser.py"],
                    "read_core_regions": [
                        {"path": "src/parser.py", "start": 4, "end": 9}
                    ],
                    "read_optional_files_map": {},
                    "read_optional_regions_map": {},
                    "modified_core_files": ["src/parser.py"],
                    "main_files": ["src/parser.py"],
                },
                "read_step_info": {},
                "meta": {},
                "dataset": "pro",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_swe_explore_cases(bench, sources=sources)

    assert len(cases) == 1
    assert cases[0].instance_id == instance_id
    assert cases[0].query == "Fix the broken parser."
    assert cases[0].base_commit == "deadbeef"


def test_loader_requires_source_identity_by_default(tmp_path: Path) -> None:
    bench = tmp_path / "bench.jsonl"
    bench.write_text(
        json.dumps(
            {
                "instance_id": "org__repo-1",
                "repo_dir": "repos/org__repo-1",
                "ground_truth": {
                    "read_core_files": ["a.py"],
                    "read_core_regions": [{"path": "a.py", "start": 1, "end": 1}],
                    "read_optional_files_map": {},
                    "read_optional_regions_map": {},
                },
                "dataset": "verified",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing SWE-Explore source record"):
        load_swe_explore_cases(bench)


def test_ground_truth_preserves_upstream_reversed_optional_regions() -> None:
    ground_truth = SWEExploreGroundTruth.from_mapping(
        {
            "read_core_files": ["a.py"],
            "read_core_regions": [{"path": "a.py", "start": 1, "end": 2}],
            "read_optional_files_map": {"model": ["a.py"]},
            "read_optional_regions_map": {
                "model": [{"path": "a.py", "start": 203, "end": 92}]
            },
            "modified_core_files": [],
            "main_files": [],
        }
    )

    assert ground_truth.read_optional_regions_map["model"] == (
        SWEExploreContextRegion("a.py", 203, 92),
    )


def test_official_metric_contract_matches_frozen_fixture(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("\n".join(f"a{i}" for i in range(1, 11)))
    (tmp_path / "b.py").write_text("\n".join(f"b{i}" for i in range(1, 6)))
    (tmp_path / "c.py").write_text("\n".join(f"c{i}" for i in range(1, 4)))
    regions = (
        SWEExploreContextRegion("a.py", 3, 4),
        SWEExploreContextRegion("a.py", 8, 9),
        SWEExploreContextRegion("c.py", 1, 2),
    )

    metrics = score_swe_explore_regions(regions, _ground_truth(), repo_path=tmp_path)

    assert tuple(metrics) == SWE_EXPLORE_METRICS
    assert metrics == pytest.approx(
        {
            "precision": 1 / 3,
            "recall": 1 / 3,
            "f1_score": 1 / 3,
            "hit_file_rate": 1 / 2,
            "noise_file_rate": 1 / 2,
            "hit_region_rate": 1 / 2,
            "noise_region_rate": 1 / 3,
            "weighted_core_coverage": 3 / 10,
            "context_efficiency": 2 / 3,
            "optional_coverage": 1.0,
            "ndcg_at_100": 1.0,
            "ndcg_at_300": 1.0,
            "ndcg_at_500": 1.0,
            "recall_at_100": 1 / 3,
            "recall_at_300": 1 / 3,
            "recall_at_500": 1 / 3,
            "first_useful_hit": 1.0,
        }
    )


def test_region_count_cutoff_is_distinct_from_line_budget(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text(
        "\n".join(f"line {line}" for line in range(1, 201)), encoding="utf-8"
    )
    ground_truth = SWEExploreGroundTruth.from_mapping(
        {
            "read_core_files": ["large.py"],
            "read_core_regions": [{"path": "large.py", "start": 1, "end": 200}],
            "read_optional_files_map": {},
            "read_optional_regions_map": {},
            "modified_core_files": ["large.py"],
            "main_files": ["large.py"],
        }
    )
    results = (
        SWEExploreResult("iid", 1.0, (SWEExploreContextRegion("large.py", 1, 150),)),
        SWEExploreResult("iid", 0.5, (SWEExploreContextRegion("large.py", 151, 200),)),
    )

    regions = flatten_swe_explore_results(results, top_k=1)
    metrics = score_swe_explore_regions(regions, ground_truth, repo_path=tmp_path)

    assert regions == (SWEExploreContextRegion("large.py", 1, 150),)
    assert metrics["recall"] == pytest.approx(0.75)
    assert metrics["recall_at_100"] == pytest.approx(0.5)
    assert metrics["recall_at_300"] == pytest.approx(0.75)


def test_prediction_record_omits_snippets_and_preserves_order() -> None:
    regions = (
        SWEExploreContextRegion("b.py", 5, 8, "secret content"),
        SWEExploreContextRegion("a.py", 1, 2),
    )

    record = swe_explore_prediction_record(
        instance_id="iid", regions=regions, explorer="codenib"
    )

    assert record == {
        "instance_id": "iid",
        "explorer": "codenib",
        "regions": [
            {"path": "b.py", "start": 5, "end": 8},
            {"path": "a.py", "start": 1, "end": 2},
        ],
        "num_regions": 2,
    }


def test_snapshot_audit_checks_commit_and_tracked_cleanliness(tmp_path: Path) -> None:
    instance_id = "org__repo-1"
    repos = tmp_path / "repos"
    repo = repos / instance_id
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    source_file = repo / "a.py"
    source_file.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = SWEExploreSourceRecord(
        instance_id=instance_id,
        repo="org/repo",
        base_commit=commit,
        problem_statement="Fix it",
        dataset="verified",
    )
    case_value = {
        "instance_id": instance_id,
        "repo_dir": f"repos/{instance_id}",
        "ground_truth": {
            "read_core_files": ["a.py"],
            "read_core_regions": [{"path": "a.py", "start": 1, "end": 1}],
            "read_optional_files_map": {},
            "read_optional_regions_map": {},
        },
        "dataset": "verified",
    }
    bench = tmp_path / "bench.jsonl"
    bench.write_text(json.dumps(case_value) + "\n", encoding="utf-8")
    case = load_swe_explore_cases(bench, sources={instance_id: source})[0]

    clean = audit_swe_explore_snapshot(case, repos)
    source_file.write_text("value = 2\n", encoding="utf-8")
    dirty = audit_swe_explore_snapshot(case, repos)

    assert clean.valid
    assert dirty.revision_matches is True
    assert dirty.is_clean is False
    assert not dirty.valid
