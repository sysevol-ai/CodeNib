# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codeminer.compiler.snapshot_store import ArtifactProfile
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
