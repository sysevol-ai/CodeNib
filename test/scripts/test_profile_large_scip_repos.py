# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for large-repo SCIP profiling manifest and gate logic."""

from __future__ import annotations

import pytest

from scripts.profiling import profile_large_scip_repos


def test_default_manifest_covers_serial_only_active_languages():
    repos = profile_large_scip_repos.load_manifest()
    by_language = {repo.language for repo in repos}

    assert {"java", "csharp", "kotlin", "scala", "php"} <= by_language
    assert "ruby" in by_language
    assert all(repo.url.startswith("https://github.com/") for repo in repos)
    assert all(repo.expected_min_source_files > 0 for repo in repos)


def test_select_repos_filters_by_name_and_language():
    repos = [
        profile_large_scip_repos.LargeScipRepo(
            name="java-one",
            language="java",
            url="https://example.invalid/java.git",
        ),
        profile_large_scip_repos.LargeScipRepo(
            name="ruby-one",
            language="ruby",
            url="https://example.invalid/ruby.git",
        ),
    ]

    assert [
        repo.name
        for repo in profile_large_scip_repos.select_repos(repos, languages=["java"])
    ] == ["java-one"]
    assert [
        repo.name
        for repo in profile_large_scip_repos.select_repos(repos, names=["ruby-one"])
    ] == ["ruby-one"]


def test_acceleration_decision_crosses_threshold_when_decode_is_hot():
    decision = profile_large_scip_repos.acceleration_decision(
        generate_seconds=6.0,
        protoc_decode_seconds=1.0,
        serial_process_seconds=3.0,
        threshold=0.20,
    )

    assert decision["crossed"] is True
    assert decision["local_decode_build_fraction"] == pytest.approx(0.30)
    assert decision["recommendation"] == "profile-core-decoder"


def test_acceleration_decision_keeps_serial_when_indexer_dominates():
    decision = profile_large_scip_repos.acceleration_decision(
        generate_seconds=90.0,
        protoc_decode_seconds=2.0,
        serial_process_seconds=8.0,
        threshold=0.20,
    )

    assert decision["crossed"] is False
    assert decision["local_decode_build_fraction"] == pytest.approx(0.08)
    assert decision["recommendation"] == "keep-serial-until-larger-profile"


def test_dry_run_plan_is_serializable():
    repos = [
        profile_large_scip_repos.LargeScipRepo(
            name="java-one",
            language="java",
            url="https://example.invalid/java.git",
            ref="main",
            target_dir="src/main/java",
        )
    ]

    plan = profile_large_scip_repos.dry_run_plan(repos, threshold=0.25)

    assert plan["mode"] == "dry-run"
    assert plan["acceleration_threshold"] == 0.25
    assert plan["repo_count"] == 1
    assert plan["repos"][0]["target_dir"] == "src/main/java"
