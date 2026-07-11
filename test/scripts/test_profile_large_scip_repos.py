# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for large-repo SCIP profiling manifest and gate logic."""

from __future__ import annotations

from types import SimpleNamespace

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


def test_ensure_checkout_applies_configured_timeout_to_fetch(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    calls = []

    def fake_run_command(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(profile_large_scip_repos, "run_command", fake_run_command)
    repo = profile_large_scip_repos.LargeScipRepo(
        name="large",
        language="java",
        url="https://example.invalid/large.git",
        ref="main",
    )

    profile_large_scip_repos.ensure_checkout(repo, repo_root, timeout=987)

    fetch = next(call for call in calls if call[0][:2] == ["git", "fetch"])
    assert fetch[1]["timeout"] == 987


def test_create_profile_indexer_forces_scip_candidate_route(tmp_path, monkeypatch):
    captured = {}
    sentinel = object()

    def fake_indexer(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr("codeminer.ls_router.LSIndexer", fake_indexer)
    repo = profile_large_scip_repos.LargeScipRepo(
        name="ruby-large",
        language="ruby",
        url="https://example.invalid/ruby.git",
        exclude_patterns=("vendor/**",),
    )

    result = profile_large_scip_repos.create_profile_indexer(
        repo,
        tmp_path,
        tmp_path / "out",
        decoder_backend="serial",
        profiler=object(),
    )

    assert result is sentinel
    assert captured["kwargs"]["graph_route"] == "scip-candidate"
    assert captured["kwargs"]["decoder_backend"] == "serial"
    assert captured["kwargs"]["exclude_patterns"] == ["vendor/**"]


def test_set_target_dir_reaches_leaf_delegate():
    leaf = SimpleNamespace(_target_dir=None)
    wrapper = SimpleNamespace(_delegate=SimpleNamespace(_delegate=leaf))

    profile_large_scip_repos.set_target_dir(wrapper, "src/main")

    assert leaf._target_dir == "src/main"


def test_process_profile_graph_excludes_artifact_persistence(tmp_path):
    saved = []
    occurrences = []

    class FakeGraph:
        lsp_occurrence_index = SimpleNamespace(
            save=lambda path: occurrences.append(path)
        )

        def save_graph(self, path):
            saved.append(path)

    graph = FakeGraph()

    class FakeIndexer:
        graph_file = tmp_path / "graph.pkl"

        def process_index(self, output_file):
            assert output_file is None
            return graph

    profile = profile_large_scip_repos.BackendProfile(backend="serial")

    result = profile_large_scip_repos.process_profile_graph(FakeIndexer(), profile)

    assert result is graph
    assert set(profile.timings) == {"process_index_total", "save_graph"}
    assert saved == [str(tmp_path / "graph.pkl")]
    assert occurrences == [tmp_path / "lsp_index.pkl"]
