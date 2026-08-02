# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codenib.compiler.artifact_quality import ARTIFACT_QUALITY_SCHEMA_VERSION
from codenib.compiler.snapshot_store import ArtifactProfile
from scripts import swebench_graph_index
from scripts.embeddings import build_embeddings


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Go", "go"),
        ("Java", "java"),
        ("Ruby", "ruby"),
        ("PHP", "php"),
        ("TypeScript/JavaScript", "typescript"),
        ("C++/C", "cpp"),
    ],
)
def test_graph_index_maps_multilingual_labels(label, expected):
    assert swebench_graph_index._map_language_label(label, "python") == expected


def test_graph_and_embedding_use_same_typescript_profile():
    graph_languages = swebench_graph_index._profile_languages("typescript")
    embedding_languages = build_embeddings._map_language_group("TypeScript/JavaScript")

    assert (
        ArtifactProfile.create(graph_languages).profile_id
        == ArtifactProfile.create(embedding_languages).profile_id
    )


def test_embedding_resolves_language_from_multilingual_repo_map():
    instance = {"repo": "google/gson"}

    assert build_embeddings._resolve_languages(
        instance,
        ["python"],
        {"google/gson": "Java"},
    ) == ["java"]


def test_graph_builder_supports_codenib_base_source(monkeypatch, tmp_path):
    captured = {}

    class FakeDataset:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(swebench_graph_index, "CodeNibBaseDataset", FakeDataset)
    args = SimpleNamespace(
        split="test",
        filter_instance=".*",
        cache_dir=str(tmp_path / "cache"),
        repo_cache_dir=str(tmp_path / "repos"),
        codenib_base_dataset="org/codenib-base",
    )

    dataset = swebench_graph_index._build_dataset("codenib_base", args)

    assert isinstance(dataset, FakeDataset)
    assert captured["dataset"] == "org/codenib-base"
    assert captured["repo_root"] == str(tmp_path / "repos")


def test_isolated_embedding_reuse_requires_passing_quality_report(tmp_path):
    root = tmp_path / "artifact"
    root.mkdir()
    model = "test/model"
    suffix = "test__model"
    instance = {
        "repo": "org/repo",
        "base_commit": "a" * 40,
    }
    (root / f"config_{suffix}.json").write_text(
        json.dumps(
            {
                "artifact": {
                    "repo": "org/repo",
                    "commit": "a" * 40,
                }
            }
        ),
        encoding="utf-8",
    )
    quality_path = root / f"artifact_quality_{suffix}.json"
    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": False,
            }
        ),
        encoding="utf-8",
    )

    assert not build_embeddings._quality_report_is_reusable(
        root,
        embedding_model=model,
        instance=instance,
    )

    artifact = {
        "repo": "org/repo",
        "commit": "a" * 40,
    }
    quality_path.write_text(
        json.dumps({"passed": True, "artifact": artifact}),
        encoding="utf-8",
    )
    assert not build_embeddings._quality_report_is_reusable(
        root,
        embedding_model=model,
        instance=instance,
    )

    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": True,
                "artifact": {**artifact, "repo": "org/other"},
            }
        ),
        encoding="utf-8",
    )
    assert not build_embeddings._quality_report_is_reusable(
        root,
        embedding_model=model,
        instance=instance,
    )

    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": True,
                "artifact": artifact,
            }
        ),
        encoding="utf-8",
    )
    assert build_embeddings._quality_report_is_reusable(
        root,
        embedding_model=model,
        instance=instance,
        expected_configuration={},
    )

    assert not build_embeddings._quality_report_is_reusable(
        root,
        embedding_model=model,
        instance=instance,
        expected_configuration={"build_levels": ["l0", "l2"]},
    )


def test_graph_reuse_requires_matching_artifact_provenance(tmp_path):
    expected = {
        "repo": "org/repo",
        "commit": "a" * 40,
        "tree": "b" * 40,
        "language": "python",
    }

    assert not swebench_graph_index._graph_quality_is_reusable(
        tmp_path,
        expected_artifact=expected,
    )

    quality_path = tmp_path / "artifact_quality.json"
    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": False,
                "artifact": expected,
            }
        ),
        encoding="utf-8",
    )
    assert not swebench_graph_index._graph_quality_is_reusable(
        tmp_path,
        expected_artifact=expected,
    )

    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": True,
                "artifact": expected,
            }
        ),
        encoding="utf-8",
    )
    assert swebench_graph_index._graph_quality_is_reusable(
        tmp_path,
        expected_artifact=expected,
    )

    changed = {**expected, "language": "go"}
    assert not swebench_graph_index._graph_quality_is_reusable(
        tmp_path,
        expected_artifact=changed,
    )
