# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codenib.compiler.artifact_quality import (
    ARTIFACT_QUALITY_SCHEMA_VERSION,
    vector_artifact_file_fingerprints,
)
from codenib.compiler.snapshot_store import ArtifactProfile
from codenib.repository_filters import REPOSITORY_FILTER_POLICY_VERSION
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


@pytest.mark.parametrize(("configured", "effective"), [(None, 300), (128, 128)])
def test_embedding_artifact_records_effective_l0_fallback_limit(configured, effective):
    args = SimpleNamespace(
        max_lines_per_chunk=configured,
        embedding_provider="huggingface",
        embedding_dimension=384,
        index_type="flat",
        index_metric="ip",
        ivf_nlist=100,
        ivf_nprobe=8,
        max_seq_length=8192,
    )

    configuration = build_embeddings._artifact_configuration(
        languages=["cpp"],
        build_levels=["l0", "l2"],
        args=args,
    )

    assert configuration["chunking"]["l0_raw_fallback_max_lines"] == effective
    assert configuration["embedding"]["model"] == "nomic-ai/CodeRankEmbed"
    assert configuration["embedding"]["revision"] == (
        "3c4b60807d71f79b43f3c4363786d9493691f8b1"
    )


def test_graph_artifact_identity_tracks_repository_filter_policy():
    metadata = swebench_graph_index._graph_artifact_metadata(
        repo="org/repo",
        surface=SimpleNamespace(commit="a" * 40, tree="b" * 40),
        language="python",
        exclude_patterns=(),
        enforce_commit_surface=True,
        source_coverage_fallback=True,
    )

    assert metadata["repository_filter_policy"] == REPOSITORY_FILTER_POLICY_VERSION


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


def test_isolated_embedding_reuse_requires_passing_quality_report(
    monkeypatch, tmp_path
):
    root = tmp_path / "artifact"
    root.mkdir()
    quality_path = tmp_path / "quality.json"
    model = "test/model"
    suffix = "test__model"
    instance = {
        "repo": "org/repo",
        "base_commit": "a" * 40,
    }
    files_match = True
    monkeypatch.setattr(
        build_embeddings,
        "vector_artifact_files_match",
        lambda *_args, **_kwargs: files_match,
    )

    def is_reusable(expected_configuration=None, required_l0_files=()):
        return build_embeddings._quality_report_is_reusable(
            root,
            quality_path=quality_path,
            embedding_model=model,
            instance=instance,
            expected_configuration=expected_configuration or {},
            build_levels=["l0"],
            required_l0_files=required_l0_files,
        )

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
    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": False,
            }
        ),
        encoding="utf-8",
    )

    assert not is_reusable()

    artifact = {
        "repo": "org/repo",
        "commit": "a" * 40,
    }
    quality_path.write_text(
        json.dumps({"passed": True, "artifact": artifact}),
        encoding="utf-8",
    )
    assert not is_reusable()

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
    assert not is_reusable()

    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": True,
                "artifact": artifact,
                "l0_files": [],
            }
        ),
        encoding="utf-8",
    )
    assert is_reusable()
    assert not is_reusable(required_l0_files=("src/required.py",))

    files_match = False
    assert not is_reusable()
    files_match = True

    short_artifact = {**artifact, "commit": "a"}
    (root / f"config_{suffix}.json").write_text(
        json.dumps({"artifact": short_artifact}),
        encoding="utf-8",
    )
    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": True,
                "artifact": short_artifact,
            }
        ),
        encoding="utf-8",
    )
    assert not is_reusable()

    (root / f"config_{suffix}.json").write_text(
        json.dumps({"artifact": artifact}),
        encoding="utf-8",
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

    assert not is_reusable(expected_configuration={"build_levels": ["l0", "l2"]})


def test_isolated_embedding_reuse_revalidates_assessed_files(tmp_path):
    root = tmp_path / "artifact"
    level = root / "l0"
    level.mkdir(parents=True)
    model = "test/model"
    suffix = "test__model"
    artifact = {"repo": "org/repo", "commit": "a" * 40}
    (root / f"config_{suffix}.json").write_text(
        json.dumps({"artifact": artifact}), encoding="utf-8"
    )
    for name in (
        f"config_{suffix}.json",
        f"index_{suffix}.faiss",
        f"documents_{suffix}.pkl",
    ):
        (level / name).write_bytes(name.encode("utf-8"))
    quality_path = tmp_path / "quality.json"
    quality_path.write_text(
        json.dumps(
            {
                "schema_version": ARTIFACT_QUALITY_SCHEMA_VERSION,
                "passed": True,
                "artifact": artifact,
                "l0_files": ["src/main.py"],
                "file_fingerprints": vector_artifact_file_fingerprints(
                    root,
                    embedding_model=model,
                    build_levels=["l0"],
                ),
            }
        ),
        encoding="utf-8",
    )

    kwargs = {
        "quality_path": quality_path,
        "embedding_model": model,
        "instance": {
            "repo": "org/repo",
            "base_commit": "a" * 40,
        },
        "expected_configuration": {},
        "build_levels": ["l0"],
        "required_l0_files": ["src/main.py"],
    }
    assert build_embeddings._quality_report_is_reusable(root, **kwargs)

    (level / f"index_{suffix}.faiss").write_bytes(b"corrupt")

    assert not build_embeddings._quality_report_is_reusable(root, **kwargs)


def test_embedding_layout_keeps_profile_metadata_outside_vector_tree(tmp_path):
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    (profile_root / "profile.json").write_text("{}\n", encoding="utf-8")
    repository = tmp_path / "repository"
    repository.mkdir()
    (profile_root / "repo").symlink_to(repository, target_is_directory=True)

    artifact_root = build_embeddings._vector_artifact_root(
        profile_root,
        None,
        "snapshot",
    )
    quality_path = build_embeddings._vector_quality_path(
        profile_root,
        None,
        "test/model",
        "snapshot",
    )
    artifact_root.mkdir()
    quality_path.parent.mkdir(parents=True)
    quality_path.write_text("{}\n", encoding="utf-8")

    assert artifact_root == profile_root / "vector"
    assert build_embeddings._vector_artifact_root(profile_root, None) == profile_root
    assert artifact_root not in quality_path.parents
    assert profile_root not in quality_path.parents
    profile_alias = tmp_path / "instance"
    profile_alias.symlink_to(profile_root, target_is_directory=True)
    assert (
        build_embeddings._vector_quality_path(
            profile_alias,
            None,
            "test/model",
            "snapshot",
        )
        == quality_path
    )
    with build_embeddings.capture_authenticated_vector_view(artifact_root) as view:
        assert view.root == artifact_root


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
