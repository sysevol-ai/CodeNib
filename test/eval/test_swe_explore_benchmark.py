# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from codenib.eval.benchmarks.swe_explore import (
    SWE_EXPLORE_METRICS,
    SWEExploreCase,
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
    submodule_origin = tmp_path / "submodule-origin"
    submodule_origin.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=submodule_origin, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=submodule_origin, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=submodule_origin,
        check=True,
    )
    (submodule_origin / "library.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "library.py"], cwd=submodule_origin, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "submodule fixture"],
        cwd=submodule_origin,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(submodule_origin),
            "deps/library",
        ],
        cwd=repo,
        check=True,
    )
    source_file = repo / "a.py"
    source_file.write_text("value = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.py\n.codenib-extra/\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py", ".gitignore"], cwd=repo, check=True)
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
    untracked_file = repo / "untracked.py"
    untracked_file.write_text("value = 2\n", encoding="utf-8")
    untracked = audit_swe_explore_snapshot(case, repos)
    untracked_file.unlink()
    ignored_file = repo / "ignored.py"
    ignored_file.write_text("value = 3\n", encoding="utf-8")
    ignored = audit_swe_explore_snapshot(case, repos)
    ignored_file.unlink()
    prefixed_dir = repo / ".codenib-extra"
    prefixed_dir.mkdir()
    (prefixed_dir / "a.py").write_text("value = 4\n", encoding="utf-8")
    prefixed = audit_swe_explore_snapshot(case, repos)
    (prefixed_dir / "a.py").unlink()
    prefixed_dir.rmdir()
    skipped_file = repo / "app.min.js"
    skipped_file.write_text("const bundled = true;\n", encoding="utf-8")
    skipped = audit_swe_explore_snapshot(case, repos)
    skipped_file.unlink()
    submodule_skipped_file = repo / "deps" / "library" / "app.min.js"
    submodule_skipped_file.write_text("const bundled = true;\n", encoding="utf-8")
    submodule_skipped = audit_swe_explore_snapshot(case, repos)
    submodule_skipped_file.unlink()
    submodule_file = repo / "deps" / "library" / "untracked.py"
    submodule_file.write_text("value = 5\n", encoding="utf-8")
    submodule = audit_swe_explore_snapshot(case, repos)
    submodule_file.unlink()
    submodule_checkout = repo / "deps" / "library"
    pinned_submodule_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=submodule_checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=submodule_checkout, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=submodule_checkout,
        check=True,
    )
    (submodule_checkout / "new.py").write_text("value = 7\n", encoding="utf-8")
    subprocess.run(["git", "add", "new.py"], cwd=submodule_checkout, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "advance submodule"],
        cwd=submodule_checkout,
        check=True,
    )
    subprocess.run(
        ["git", "config", "submodule.deps/library.ignore", "all"],
        cwd=repo,
        check=True,
    )
    submodule_revision = audit_swe_explore_snapshot(case, repos)
    subprocess.run(
        ["git", "reset", "--hard", "-q", pinned_submodule_commit],
        cwd=submodule_checkout,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--unset", "submodule.deps/library.ignore"],
        cwd=repo,
        check=True,
    )
    embedded_repo = repo / "embedded"
    embedded_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=embedded_repo, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=embedded_repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=embedded_repo,
        check=True,
    )
    (embedded_repo / "nested.py").write_text("value = 6\n", encoding="utf-8")
    subprocess.run(["git", "add", "nested.py"], cwd=embedded_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "embedded fixture"],
        cwd=embedded_repo,
        check=True,
    )
    embedded = audit_swe_explore_snapshot(case, repos)
    shutil.rmtree(embedded_repo)
    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "a.py"], cwd=repo, check=True
    )
    source_file.write_text("value = 8\n", encoding="utf-8")
    assume_unchanged = audit_swe_explore_snapshot(case, repos)
    subprocess.run(
        ["git", "update-index", "--no-assume-unchanged", "a.py"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "update-index", "--skip-worktree", "a.py"], cwd=repo, check=True
    )
    source_file.write_text("value = 9\n", encoding="utf-8")
    skip_worktree = audit_swe_explore_snapshot(case, repos)
    subprocess.run(
        ["git", "update-index", "--no-skip-worktree", "a.py"], cwd=repo, check=True
    )
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "update-index", "--skip-worktree", "a.py"], cwd=repo, check=True
    )
    source_file.unlink()
    sparse_source = audit_swe_explore_snapshot(case, repos)
    subprocess.run(
        ["git", "update-index", "--no-skip-worktree", "a.py"], cwd=repo, check=True
    )
    subprocess.run(["git", "checkout", "--", "a.py"], cwd=repo, check=True)
    gitignore = repo / ".gitignore"
    subprocess.run(
        ["git", "update-index", "--skip-worktree", ".gitignore"],
        cwd=repo,
        check=True,
    )
    gitignore.unlink()
    sparse_non_source = audit_swe_explore_snapshot(case, repos)
    subprocess.run(
        ["git", "update-index", "--no-skip-worktree", ".gitignore"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "--", ".gitignore"], cwd=repo, check=True)
    source_file.write_text("value = 2\n", encoding="utf-8")
    dirty = audit_swe_explore_snapshot(case, repos)

    assert clean.valid
    assert clean.unexpected_source_files == ()
    assert clean.suppressed_source_files == ()
    assert clean.submodule_revisions_match is True
    assert untracked.unexpected_source_files == ("untracked.py",)
    assert not untracked.valid
    assert ignored.unexpected_source_files == ("ignored.py",)
    assert not ignored.valid
    assert prefixed.unexpected_source_files == (".codenib-extra/a.py",)
    assert not prefixed.valid
    assert skipped.unexpected_source_files == ()
    assert skipped.valid
    assert submodule_skipped.unexpected_source_files == ()
    assert submodule_skipped.valid
    assert submodule.unexpected_source_files == ("deps/library/untracked.py",)
    assert not submodule.valid
    assert submodule_revision.submodule_revisions_match is False
    assert not submodule_revision.valid
    assert embedded.unexpected_source_files == ("embedded/nested.py",)
    assert not embedded.valid
    assert assume_unchanged.suppressed_source_files == ("a.py",)
    assert not assume_unchanged.valid
    assert skip_worktree.suppressed_source_files == ("a.py",)
    assert not skip_worktree.valid
    assert sparse_source.suppressed_source_files == ("a.py",)
    assert not sparse_source.valid
    assert sparse_non_source.suppressed_source_files == ()
    assert sparse_non_source.valid
    assert dirty.revision_matches is True
    assert dirty.is_clean is False
    assert not dirty.valid


def test_snapshot_audit_rejects_tracked_source_symlink_outside_checkout(
    tmp_path: Path,
) -> None:
    instance_id = "org__repo-1"
    repos = tmp_path / "repos"
    repo = repos / instance_id
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    external = tmp_path / "external.py"
    external.write_text("value = 1\n", encoding="utf-8")
    (repo / "linked.py").symlink_to(external)
    subprocess.run(["git", "add", "linked.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "symlink fixture"], cwd=repo, check=True)
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
    case = SWEExploreCase(
        instance_id=instance_id,
        dataset="verified",
        repo_dir=f"repos/{instance_id}",
        ground_truth=_ground_truth(),
        source=source,
    )

    audit = audit_swe_explore_snapshot(case, repos)

    assert audit.revision_matches is True
    assert audit.unexpected_source_files == ("linked.py",)
    assert audit.is_clean is False
    assert not audit.valid
